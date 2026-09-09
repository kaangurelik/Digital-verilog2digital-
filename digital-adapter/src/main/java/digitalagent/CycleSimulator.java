package digitalagent;

import de.neemann.digital.core.Bits;
import de.neemann.digital.core.Model;
import de.neemann.digital.core.ModelEventType;
import de.neemann.digital.core.ObservableValue;
import de.neemann.digital.core.flipflops.FlipflopD;
import de.neemann.digital.draw.elements.Circuit;
import de.neemann.digital.draw.elements.Pin;
import de.neemann.digital.draw.elements.VisualElement;
import de.neemann.digital.draw.model.ModelCreator;
import de.neemann.digital.draw.model.ModelEntry;
import de.neemann.digital.draw.model.Net;
import de.neemann.digital.draw.model.NetList;
import org.json.JSONArray;
import org.json.JSONObject;

import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.function.LongSupplier;

import static digitalagent.JsonSupport.*;

/** Single-clock simulation through public input pins, without internal state writes. */
final class CycleSimulator {
    static final int MAX_PREFIX_CYCLES = 256;
    static final int MAX_CYCLES = 1024;
    static final int MAX_RESPONSE_BYTES = 6 * 1024 * 1024;
    static final long TIMEOUT_NANOS = 15_000_000_000L;
    static final List<String> PHASES = List.of("before_rise", "after_rise", "after_fall");
    private final Components components;
    private final LongSupplier clock;
    private final long timeout;

    CycleSimulator(Components components) {
        this(components, System::nanoTime, TIMEOUT_NANOS);
    }

    CycleSimulator(Components components, LongSupplier clock, long timeout) {
        this.components = components;
        this.clock = clock;
        this.timeout = timeout;
    }

    JSONObject simulate(Circuit circuit, Map<VisualElement, String> ids, JSONObject params) throws Exception {
        fields(params, "path", "sidecar_path", "clock_instance_id", "initialization", "cycles");
        String clockId = string(params, "clock_instance_id");
        JSONObject initialization = object(params, "initialization");
        fields(initialization, "kind", "cycles");
        if (!"input_sequence".equals(string(initialization, "kind"))) {
            throw invalid("INVALID_INITIALIZATION", "Only an explicit input_sequence prefix is supported");
        }
        JSONArray prefix = array(initialization, "cycles", MAX_PREFIX_CYCLES);
        JSONArray cycles = array(params, "cycles", MAX_CYCLES);
        if (prefix.isEmpty() || cycles.isEmpty()) {
            throw invalid("EMPTY_CYCLE_SEQUENCE", "Provide at least one prefix cycle and one observed cycle");
        }
        long deadline = clock.getAsLong() + timeout;
        checkDeadline(deadline);
        NetList nets = new NetList(circuit);
        ModelCreator creator = new ModelCreator(circuit, components.library, false, nets, "", 0, null);
        TreeMap<String, ModelEntry> entries = new TreeMap<>();
        TreeMap<String, ObservableValue> inputs = new TreeMap<>(), outputs = new TreeMap<>();
        JSONObject defaults = new JSONObject();
        for (ModelEntry entry : creator) {
            String id = ids.get(entry.getVisualElement());
            if (id == null || entries.put(id, entry) != null) {
                throw invalid("INVALID_COMPONENT_IDENTITY", "Simulation requires unique tracked native components");
            }
            String type = entry.getVisualElement().getElementName();
            if (type.equals("In")) inputs.put(id, entry.getElement().getOutputs().get(0));
            if (type.equals("D_FF")) {
                if (!(entry.getElement() instanceof FlipflopD register)) {
                    throw invalid("UNSUPPORTED_REGISTER", "D_FF must use the supported native rising-edge implementation");
                }
                defaults.put(id, hex(register.getDefault() & Bits.mask(register.getDataBits())));
            }
        }
        if (defaults.isEmpty()) throw invalid("NO_REGISTERS", "Cycle simulation requires at least one D_FF");
        ObservableValue clockInput = inputs.get(clockId);
        if (clockInput == null || clockInput.getBits() != 1) {
            throw invalid("INVALID_CLOCK_INPUT", "clock_instance_id must identify a scalar native In component");
        }
        validateClock(entries, nets, clockId);
        for (Net net : nets) {
            int drivers = 0;
            for (Pin pin : net.getPins()) if (pin.getDirection() != Pin.Direction.input) drivers++;
            if (drivers > 1) throw invalid("MULTIPLE_DRIVERS", "Binary simulation requires at most one driver per net");
        }
        TreeMap<String, ObservableValue> dataInputs = new TreeMap<>(inputs);
        dataInputs.remove(clockId);
        List<Map<String, Long>> prefixValues = readCycles(prefix, dataInputs);
        List<Map<String, Long>> cycleValues = readCycles(cycles, dataInputs);
        Model model = null;
        try {
            model = creator.createModel(true).setRecoverFromOscillation(false);
            ArrayList<Exception> errors = new ArrayList<>();
            model.addObserver(event -> {
                if (event.getType() == ModelEventType.ERROR_OCCURRED) {
                    errors.add(event.getCause() == null ? new IllegalStateException("Native model reported an error") : event.getCause());
                }
            }, ModelEventType.ERROR_OCCURRED);
            for (Map.Entry<String, ModelEntry> item : entries.entrySet()) {
                ModelEntry entry = item.getValue();
                if (entry.getVisualElement().getElementName().equals("Out")) {
                    outputs.put(item.getKey(), entry.getPins().getInputs().get("in").getValue());
                }
            }
            if (outputs.isEmpty()) throw invalid("NO_OUTPUTS", "Cycle simulation requires at least one output");
            JSONObject assumptions = new JSONObject().put("profile", "single_clock_rising_v1")
                    .put("clock_instance_id", clockId).put("initial_clock", "0x0")
                    .put("phase_order", new JSONArray(PHASES)).put("input_application", "settled_while_clock_low")
                    .put("state_updates", "simultaneous").put("noise", false)
                    .put("initialization", new JSONObject().put("kind", "input_sequence").put("prefix_cycles", prefix.length())
                            .put("internal_state_forcing", false).put("native_defaults", defaults));
            checkResponseBudget(outputs, cycles.length(), assumptions);
            // Initialize through top-level inputs, preserving serialized D_FF defaults.
            // The caller proves this prefix establishes known HDL state.
            clockInput.setValue(0);
            apply(dataInputs, prefixValues.get(0));
            model.init(false);
            checkRunning(model, errors);
            checkDeadline(deadline);
            for (Map<String, Long> row : prefixValues) {
                runCycle(model, errors, clockInput, dataInputs, outputs, row, null, -1, deadline);
            }
            JSONArray samples = new JSONArray();
            for (int i = 0; i < cycleValues.size(); i++) {
                runCycle(model, errors, clockInput, dataInputs, outputs, cycleValues.get(i), samples, i, deadline);
            }
            JSONObject result = new JSONObject().put("schema_version", 1).put("samples", samples).put("assumptions", assumptions);
            if (result.toString().getBytes(StandardCharsets.UTF_8).length > MAX_RESPONSE_BYTES) {
                throw invalid("SIMULATION_OUTPUT_LIMIT", "Cycle simulation response exceeds the 6 MiB limit");
            }
            checkDeadline(deadline);
            return result;
        } finally {
            if (model != null) model.close();
        }
    }

    private static void validateClock(Map<String, ModelEntry> entries, NetList nets, String clockId) throws RpcException {
        ModelEntry source = entries.get(clockId);
        Pin sourcePin = null;
        for (Pin pin : source.getPins()) {
            if (pin.getDirection() == Pin.Direction.output && pin.getName().equals("out")) sourcePin = pin;
        }
        if (sourcePin == null) throw invalid("INVALID_CLOCK_INPUT", "The requested clock has no native output pin");
        Net clockNet = nets.getNetOfPin(sourcePin);
        if (clockNet == null) throw invalid("DISCONNECTED_CLOCK", "The requested clock is not connected");
        for (Map.Entry<String, ModelEntry> item : entries.entrySet()) {
            ModelEntry entry = item.getValue();
            String type = entry.getVisualElement().getElementName();
            if (type.equals("D_FF") && nets.getNetOfPin(entry.getPins().getInputs().get("C")) != clockNet) {
                throw invalid("UNSUPPORTED_CLOCK", "Every D_FF.C must share the requested direct input clock net");
            }
            for (Pin pin : entry.getPins()) {
                if (nets.getNetOfPin(pin) != clockNet) continue;
                boolean driver = item.getKey().equals(clockId) && pin == sourcePin;
                boolean registerClock = type.equals("D_FF") && pin.getDirection() == Pin.Direction.input && pin.getName().equals("C");
                boolean transparentTunnel = type.equals("Tunnel") && pin.getDirection() == Pin.Direction.input;
                if (!driver && !registerClock && !transparentTunnel) {
                    throw invalid("CLOCK_USED_AS_DATA", "The clock net may connect only its In source, D_FF.C pins, and transparent tunnels");
                }
            }
        }
    }

    private static List<Map<String, Long>> readCycles(JSONArray cycles, Map<String, ObservableValue> inputs) throws RpcException {
        ArrayList<Map<String, Long>> result = new ArrayList<>();
        for (int i = 0; i < cycles.length(); i++) {
            JSONObject cycle = arrayObject(cycles, i);
            fields(cycle, "values");
            JSONObject values = object(cycle, "values");
            if (!values.keySet().equals(inputs.keySet())) {
                throw invalid("INPUT_SET_MISMATCH", "Every cycle must supply exactly the nonclock input instance IDs: " + inputs.keySet());
            }
            TreeMap<String, Long> row = new TreeMap<>();
            for (Map.Entry<String, ObservableValue> input : inputs.entrySet()) {
                String value = string(values, input.getKey());
                if (!value.matches("0[xX][0-9a-fA-F]{1,16}")) {
                    throw invalid("INVALID_SIGNAL_VALUE", "Cycle inputs must be uint64 hexadecimal strings");
                }
                BigInteger parsed = new BigInteger(value.substring(2), 16);
                if (parsed.bitLength() > input.getValue().getBits()) {
                    throw invalid("SIGNAL_WIDTH", "Cycle input exceeds its native width: " + input.getKey());
                }
                row.put(input.getKey(), parsed.longValue());
            }
            result.add(row);
        }
        return result;
    }

    private static void apply(Map<String, ObservableValue> inputs, Map<String, Long> values) {
        for (Map.Entry<String, ObservableValue> input : inputs.entrySet()) input.getValue().setValue(values.get(input.getKey()));
    }

    private void runCycle(Model model, List<Exception> errors, ObservableValue clockInput,
                          Map<String, ObservableValue> inputs, Map<String, ObservableValue> outputs,
                          Map<String, Long> values, JSONArray samples, int cycle, long deadline) throws RpcException {
        checkDeadline(deadline);
        // Settle data while the clock is low before all flip-flops sample D.
        clockInput.setValue(0);
        apply(inputs, values);
        settle(model, errors, deadline);
        if (samples != null) samples.put(sample(outputs, cycle, PHASES.get(0)));
        clockInput.setValue(1);
        settle(model, errors, deadline);
        if (samples != null) samples.put(sample(outputs, cycle, PHASES.get(1)));
        clockInput.setValue(0);
        settle(model, errors, deadline);
        if (samples != null) samples.put(sample(outputs, cycle, PHASES.get(2)));
    }

    private void settle(Model model, List<Exception> errors, long deadline) throws RpcException {
        checkDeadline(deadline);
        model.doStep(false);
        checkRunning(model, errors);
        checkDeadline(deadline);
    }

    private static JSONObject sample(Map<String, ObservableValue> outputs, int cycle, String phase) {
        JSONObject values = new JSONObject(), highZ = new JSONObject();
        for (Map.Entry<String, ObservableValue> output : outputs.entrySet()) {
            // Never ask ObservableValue.getValue() to randomize unresolved Z bits.
            values.put(output.getKey(), hex(output.getValue().getValueHighZIsZero()));
            highZ.put(output.getKey(), hex(output.getValue().getHighZ()));
        }
        return new JSONObject().put("cycle", cycle).put("phase", phase).put("values", values).put("high_z", highZ);
    }

    private static void checkResponseBudget(Map<String, ObservableValue> outputs, int cycles, JSONObject assumptions) throws RpcException {
        long perSample = 128;
        for (String id : outputs.keySet()) perSample += 2L * (JSONObject.quote(id).getBytes(StandardCharsets.UTF_8).length + 24);
        long estimate = assumptions.toString().getBytes(StandardCharsets.UTF_8).length + 1024L + 3L * cycles * perSample;
        if (estimate > MAX_RESPONSE_BYTES) {
            throw invalid("SIMULATION_OUTPUT_LIMIT", "Reduce observed cycles or output count to fit the 6 MiB response limit");
        }
    }

    private static String hex(long value) { return "0x" + Long.toUnsignedString(value, 16); }

    private static void checkRunning(Model model, List<Exception> errors) throws RpcException {
        if (!model.isRunning() || !errors.isEmpty()) {
            throw new RpcException(-32010, "SIMULATION_FAILED", errors.isEmpty()
                    ? "Digital closed the sequential model while settling" : String.valueOf(errors.get(0).getMessage()));
        }
    }

    private void checkDeadline(long deadline) throws RpcException {
        if (Thread.currentThread().isInterrupted() || clock.getAsLong() - deadline >= 0) {
            throw new RpcException(-32010, "SIMULATION_TIMEOUT", "Cycle simulation exceeded its 15-second deadline");
        }
    }
}
