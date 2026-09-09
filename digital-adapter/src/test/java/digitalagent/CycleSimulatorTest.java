package digitalagent;

import de.neemann.digital.draw.elements.Circuit;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import java.io.File;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.Assert.*;

/** Every observed sequential result is produced by emitted/reloaded native components. */
public class CycleSimulatorTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder(new File("target").getAbsoluteFile());
    private final DigitalAdapter adapter = new DigitalAdapter();
    private int serial;

    private static JSONObject instance(String id, String type, int bits) {
        return new JSONObject().put("id", id).put("type", type).put("attributes", new JSONObject().put("Bits", bits))
                .put("position", new JSONObject().put("x", 0).put("y", 0)).put("rotation", 0);
    }

    private static JSONObject endpoint(String id, String direction, String pin) {
        return new JSONObject().put("instance_id", id).put("direction", direction).put("pin", pin);
    }

    private static JSONObject row(Object... pairs) {
        JSONObject values = new JSONObject();
        for (int i = 0; i < pairs.length; i += 2) values.put((String) pairs[i], pairs[i + 1]);
        return new JSONObject().put("values", values);
    }

    private final class Fixture {
        final JSONArray instances = new JSONArray();
        final Map<String, JSONObject> specs = new LinkedHashMap<>();
        final Map<String, JSONObject[]> nets = new LinkedHashMap<>();

        Fixture add(String id, String type, int bits) {
            JSONObject item = instance(id, type, bits);
            item.getJSONObject("position").put("x", specs.size() * 800);
            instances.put(item);
            specs.put(id, item);
            return this;
        }

        Fixture connect(String name, JSONObject... pins) {
            nets.put(name, pins);
            return this;
        }

        JSONObject emit() throws Exception {
            JSONArray placed = new JSONArray(instances.toString()), wires = new JSONArray();
            int tunnelIndex = 0;
            for (Map.Entry<String, JSONObject[]> net : nets.entrySet()) {
                for (JSONObject endpoint : net.getValue()) {
                    JSONObject description = adapter.dispatch("component.describe", specs.get(endpoint.getString("instance_id")));
                    JSONObject pin = null;
                    for (Object item : description.getJSONArray("pins")) {
                        JSONObject found = (JSONObject) item;
                        if (found.getString("direction").equals(endpoint.getString("direction"))
                                && found.getString("pin").equals(endpoint.getString("pin"))) pin = found;
                    }
                    assertNotNull("Fixture references a real native pin", pin);
                    JSONObject p = pin.getJSONObject("position"), escape = pin.getJSONObject("escape");
                    JSONObject exit = new JSONObject().put("x", p.getInt("x") + 60 * escape.getInt("x"))
                            .put("y", p.getInt("y") + 60 * escape.getInt("y"));
                    int rotation = escape.getInt("x") == 1 ? 0 : escape.getInt("x") == -1 ? 180
                            : escape.getInt("y") == -1 ? 90 : 270;
                    placed.put(new JSONObject().put("id", "t" + tunnelIndex++).put("type", "Tunnel")
                            .put("attributes", new JSONObject().put("NetName", net.getKey())).put("position", exit).put("rotation", rotation));
                    wires.put(new JSONObject().put("from", new JSONObject(p.toString())).put("to", exit).put("net_id", net.getKey()));
                }
            }
            JSONObject result = adapter.dispatch("circuit.emit", new JSONObject().put("circuit", new JSONObject()
                            .put("schema_version", 1).put("instances", placed).put("wires", wires))
                    .put("path", temporary.getRoot().toPath().resolve("cycles-" + serial++ + ".dig").toString()));
            return new JSONObject().put("path", result.getString("path")).put("sidecar_path", result.getString("sidecar_path"));
        }
    }

    private Fixture register(int bits) {
        return new Fixture().add("clock", "In", 1).add("data", "In", bits).add("ff", "D_FF", bits)
                .add("q", "Out", bits).add("qn", "Out", bits)
                .connect("clock_net", endpoint("clock", "output", "out"), endpoint("ff", "input", "C"))
                .connect("data_net", endpoint("data", "output", "out"), endpoint("ff", "input", "D"))
                .connect("q_net", endpoint("ff", "output", "Q"), endpoint("q", "input", "in"))
                .connect("qn_net", endpoint("ff", "output", "~Q"), endpoint("qn", "input", "in"));
    }

    private static JSONObject request(JSONObject paths, JSONObject prefix, JSONObject... cycles) {
        return new JSONObject(paths.toString()).put("clock_instance_id", "clock")
                .put("initialization", new JSONObject().put("kind", "input_sequence").put("cycles", new JSONArray().put(prefix)))
                .put("cycles", new JSONArray(cycles));
    }

    private JSONObject simulate(JSONObject request) throws Exception {
        String before = JsonSupport.canonical(request);
        JSONObject result = adapter.dispatch("circuit.simulate_cycles", request);
        assertEquals(before, JsonSupport.canonical(request));
        JSONArray samples = result.getJSONArray("samples");
        assertEquals(request.getJSONArray("cycles").length() * 3, samples.length());
        for (int i = 0; i < samples.length(); i++) {
            JSONObject sample = samples.getJSONObject(i);
            assertEquals(i / 3, sample.getInt("cycle"));
            assertEquals(CycleSimulator.PHASES.get(i % 3), sample.getString("phase"));
            for (String key : sample.getJSONObject("high_z").keySet()) assertEquals("0x0", sample.getJSONObject("high_z").getString(key));
        }
        JSONObject assumptions = result.getJSONObject("assumptions");
        assertEquals("clock", assumptions.getString("clock_instance_id"));
        assertEquals("0x0", assumptions.getString("initial_clock"));
        assertFalse(assumptions.getBoolean("noise"));
        assertFalse(assumptions.getJSONObject("initialization").getBoolean("internal_state_forcing"));
        return result;
    }

    private static String value(JSONObject result, int cycle, int phase, String output) {
        return result.getJSONArray("samples").getJSONObject(cycle * 3 + phase).getJSONObject("values").getString(output);
    }

    private void reject(String code, JSONObject request) throws Exception {
        try {
            adapter.dispatch("circuit.simulate_cycles", request);
            fail("Invalid cycle simulation request accepted");
        } catch (RpcException error) {
            assertEquals(code, error.code);
        }
    }

    @Test
    public void explicitPrefixOverridesSerializedDefaultThroughInputPinsAndStateDoesNotLeakAcrossCalls() throws Exception {
        Fixture fixture = register(8);
        fixture.specs.get("ff").getJSONObject("attributes").put("Default", "0xab");
        JSONObject request = request(fixture.emit(), row("data", "0x12"), row("data", "0x34"), row("data", "0x56"));
        JSONObject result = simulate(request);
        assertEquals("0xab", result.getJSONObject("assumptions").getJSONObject("initialization").getJSONObject("native_defaults").getString("ff"));
        assertEquals("0x12", value(result, 0, 0, "q"));
        assertEquals("0x34", value(result, 0, 1, "q"));
        assertEquals("0x34", value(result, 0, 2, "q"));
        assertEquals("0xcb", value(result, 0, 1, "qn"));
        assertEquals("0x34", value(result, 1, 0, "q"));
        assertEquals("0x56", value(result, 1, 1, "q"));
        assertEquals(JsonSupport.canonical(result), JsonSupport.canonical(simulate(request)));
    }

    @Test
    public void native64BitStatePreservesUnsignedExtremesAndComplement() throws Exception {
        JSONObject result = simulate(request(register(64).emit(), row("data", "0x8000000000000000"),
                row("data", "0xffffffffffffffff"), row("data", "0x1")));
        assertEquals("0x8000000000000000", value(result, 0, 0, "q"));
        assertEquals("0x7fffffffffffffff", value(result, 0, 0, "qn"));
        assertEquals("0xffffffffffffffff", value(result, 0, 1, "q"));
        assertEquals("0x0", value(result, 0, 2, "qn"));
        assertEquals("0x1", value(result, 1, 1, "q"));
        assertEquals("0xfffffffffffffffe", value(result, 1, 2, "qn"));
    }

    @Test
    public void cascadedRegistersCaptureOldStateSimultaneously() throws Exception {
        Fixture fixture = new Fixture().add("clock", "In", 1).add("data", "In", 8)
                .add("first", "D_FF", 8).add("second", "D_FF", 8).add("a", "Out", 8).add("b", "Out", 8)
                .connect("clock_net", endpoint("clock", "output", "out"), endpoint("first", "input", "C"), endpoint("second", "input", "C"))
                .connect("data_net", endpoint("data", "output", "out"), endpoint("first", "input", "D"))
                .connect("a_net", endpoint("first", "output", "Q"), endpoint("second", "input", "D"), endpoint("a", "input", "in"))
                .connect("b_net", endpoint("second", "output", "Q"), endpoint("b", "input", "in"));
        JSONObject result = simulate(request(fixture.emit(), row("data", "0x0"), row("data", "0x11"), row("data", "0x22")));
        assertEquals("0x11", value(result, 0, 1, "a"));
        assertEquals("0x0", value(result, 0, 1, "b"));
        assertEquals("0x11", value(result, 1, 0, "a"));
        assertEquals("0x0", value(result, 1, 0, "b"));
        assertEquals("0x22", value(result, 1, 1, "a"));
        assertEquals("0x11", value(result, 1, 1, "b"));
    }

    @Test
    public void feedbackEnableAndNonzeroSynchronousResetRespectPriorityAndHold() throws Exception {
        Fixture fixture = new Fixture().add("clock", "In", 1).add("data", "In", 8).add("enable", "In", 1)
                .add("reset", "In", 1).add("ff", "D_FF", 8).add("enable_mux", "Multiplexer", 8)
                .add("reset_mux", "Multiplexer", 8).add("reset_value", "Const", 8).add("q", "Out", 8)
                .connect("clock_net", endpoint("clock", "output", "out"), endpoint("ff", "input", "C"))
                .connect("data_net", endpoint("data", "output", "out"), endpoint("enable_mux", "input", "in_1"))
                .connect("enable_net", endpoint("enable", "output", "out"), endpoint("enable_mux", "input", "sel"))
                .connect("reset_net", endpoint("reset", "output", "out"), endpoint("reset_mux", "input", "sel"))
                .connect("q_net", endpoint("ff", "output", "Q"), endpoint("q", "input", "in"), endpoint("enable_mux", "input", "in_0"))
                .connect("enable_value", endpoint("enable_mux", "output", "out"), endpoint("reset_mux", "input", "in_0"))
                .connect("reset_constant", endpoint("reset_value", "output", "out"), endpoint("reset_mux", "input", "in_1"))
                .connect("d_net", endpoint("reset_mux", "output", "out"), endpoint("ff", "input", "D"));
        fixture.specs.get("reset_value").getJSONObject("attributes").put("Value", "0xa5");
        JSONObject result = simulate(request(fixture.emit(), row("data", "0xff", "enable", "0x0", "reset", "0x1"),
                row("data", "0x33", "enable", "0x0", "reset", "0x0"),
                row("data", "0x44", "enable", "0x1", "reset", "0x0"),
                row("data", "0x55", "enable", "0x1", "reset", "0x1")));
        assertEquals("0xa5", value(result, 0, 0, "q"));
        assertEquals("0xa5", value(result, 0, 1, "q"));
        assertEquals("0xa5", value(result, 1, 0, "q"));
        assertEquals("0x44", value(result, 1, 1, "q"));
        assertEquals("0x44", value(result, 2, 0, "q"));
        assertEquals("0xa5", value(result, 2, 1, "q"));
        assertEquals("0xa5", value(result, 2, 2, "q"));
    }

    @Test
    public void rejectsClockDataUseBusClocksGatedClocksAndMultipleClockNets() throws Exception {
        JSONObject valid = request(register(8).emit(), row("data", "0x0"), row("data", "0x1"));
        reject("INVALID_CLOCK_INPUT", new JSONObject(valid.toString()).put("clock_instance_id", "q"));
        Fixture busClock = register(8);
        busClock.specs.get("clock").getJSONObject("attributes").put("Bits", 8);
        reject("INVALID_CLOCK_INPUT", request(busClock.emit(), row("data", "0x0"), row("data", "0x1")));
        Fixture clockAsData = new Fixture().add("clock", "In", 1).add("ff", "D_FF", 1).add("q", "Out", 1)
                .connect("clock_net", endpoint("clock", "output", "out"), endpoint("ff", "input", "C"), endpoint("ff", "input", "D"))
                .connect("q_net", endpoint("ff", "output", "Q"), endpoint("q", "input", "in"));
        reject("CLOCK_USED_AS_DATA", request(clockAsData.emit(), row(), row()));
        Fixture gated = register(1).add("zz_gate", "Not", 1);
        gated.connect("clock_net", endpoint("clock", "output", "out"), endpoint("zz_gate", "input", "in"))
                .connect("gated_net", endpoint("zz_gate", "output", "out"), endpoint("ff", "input", "C"));
        reject("UNSUPPORTED_CLOCK", request(gated.emit(), row("data", "0x0"), row("data", "0x1")));
        Fixture multiple = register(1).add("other_clock", "In", 1).add("second", "D_FF", 1);
        multiple.connect("clock_two", endpoint("other_clock", "output", "out"), endpoint("second", "input", "C"))
                .connect("data_net", endpoint("data", "output", "out"), endpoint("ff", "input", "D"), endpoint("second", "input", "D"));
        reject("UNSUPPORTED_CLOCK", request(multiple.emit(), row("data", "0x0", "other_clock", "0x0"), row("data", "0x1", "other_clock", "0x0")));
    }

    @Test
    public void malformedAndOversizedTracesCannotDriveAnImplicitClockOrForceState() throws Exception {
        JSONObject valid = request(register(8).emit(), row("data", "0x0"), row("data", "0x1"));
        JSONObject bad = new JSONObject(valid.toString());
        bad.getJSONObject("initialization").put("kind", "force_registers");
        reject("INVALID_INITIALIZATION", bad);
        bad = new JSONObject(valid.toString());
        bad.getJSONObject("initialization").put("native_state", new JSONObject());
        reject("UNKNOWN_FIELD", bad);
        for (String value : new String[]{"-1", "1", "0xfffffffffffffffff"}) {
            bad = new JSONObject(valid.toString()).put("cycles", new JSONArray().put(row("data", value)));
            reject("INVALID_SIGNAL_VALUE", bad);
        }
        reject("SIGNAL_WIDTH", new JSONObject(valid.toString()).put("cycles", new JSONArray().put(row("data", "0x100"))));
        reject("INVALID_STRING", new JSONObject(valid.toString()).put("cycles", new JSONArray().put(row("data", true))));
        reject("INPUT_SET_MISMATCH", new JSONObject(valid.toString()).put("cycles", new JSONArray().put(row("data", "0x0", "clock", "0x1"))));
        reject("INPUT_SET_MISMATCH", new JSONObject(valid.toString()).put("cycles", new JSONArray().put(row())));
        bad = new JSONObject(valid.toString());
        bad.getJSONObject("initialization").put("cycles", new JSONArray());
        reject("EMPTY_CYCLE_SEQUENCE", bad);
        reject("EMPTY_CYCLE_SEQUENCE", new JSONObject(valid.toString()).put("cycles", new JSONArray()));
        JSONArray tooMany = new JSONArray();
        for (int i = 0; i < 1025; i++) tooMany.put(row("data", "0x0"));
        reject("RESOURCE_LIMIT", new JSONObject(valid.toString()).put("cycles", tooMany));
        // The worker remains usable after all rejected requests.
        assertEquals("0x1", value(simulate(valid), 0, 1, "q"));
    }

    @Test
    public void responseAndExecutionBudgetsFailBeforeLargeSimulation() throws Exception {
        Fixture fixture = register(8);
        JSONObject[] pins = new JSONObject[66];
        pins[0] = endpoint("ff", "output", "Q");
        pins[1] = endpoint("q", "input", "in");
        for (int i = 0; i < 64; i++) {
            String id = "output_" + i;
            fixture.add(id, "Out", 8);
            pins[i + 2] = endpoint(id, "input", "in");
        }
        // Keep the pre-existing q output connected too.
        fixture.connect("q_net", pins);
        JSONObject request = request(fixture.emit(), row("data", "0x0"), row("data", "0x1"));
        JSONArray cycles = new JSONArray();
        for (int i = 0; i < 1024; i++) cycles.put(row("data", "0x0"));
        request.put("cycles", cycles);
        reject("SIMULATION_OUTPUT_LIMIT", request);
        try {
            new CycleSimulator(new Components(), () -> 0L, 0).simulate(new Circuit(), Map.of(), request);
            fail("Expired simulation was started");
        } catch (RpcException error) {
            assertEquals("SIMULATION_TIMEOUT", error.code);
        }
    }
}
