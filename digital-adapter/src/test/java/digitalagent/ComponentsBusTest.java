package digitalagent;

import de.neemann.digital.core.Model;
import de.neemann.digital.core.Node;
import de.neemann.digital.core.ObservableValue;
import de.neemann.digital.core.ObservableValues;
import de.neemann.digital.core.element.Element;
import de.neemann.digital.core.element.ElementAttributes;
import de.neemann.digital.core.element.ElementTypeDescription;
import de.neemann.digital.core.element.PinDescription;
import de.neemann.digital.draw.elements.VisualElement;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Test;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;

import static org.junit.Assert.*;

/** Native interface and arithmetic checks, including the 64-bit boundary. */
public class ComponentsBusTest {
    private final Components components = new Components();
    private final ArrayList<Model> models = new ArrayList<>();

    @After
    public void closeModels() {
        for (Model model : models) model.close();
    }

    private JSONObject request(String type, JSONObject attributes, int rotation) {
        return new JSONObject().put("type", type).put("attributes", attributes).put("rotation", rotation)
                .put("position", new JSONObject().put("x", 100).put("y", -200));
    }

    private Map<String, Integer> widths(JSONObject description) {
        Map<String, Integer> result = new HashMap<>();
        for (Object object : description.getJSONArray("pins")) {
            JSONObject pin = (JSONObject) object;
            String identity = pin.getString("direction") + ":" + pin.getString("pin");
            assertFalse("Native pin identities must be distinct: " + identity, result.containsKey(identity));
            int width = pin.getInt("width");
            assertTrue(identity, width >= 1 && width <= 64);
            result.put(identity, width);
        }
        return result;
    }

    private static final class Harness {
        final Element element;
        final Model model;
        final Map<String, ObservableValue> inputs;
        final Map<String, ObservableValue> outputs;

        Harness(Element element, Model model, Map<String, ObservableValue> inputs, Map<String, ObservableValue> outputs) {
            this.element = element;
            this.model = model;
            this.inputs = inputs;
            this.outputs = outputs;
        }

        void evaluate(Map<String, Long> values) throws Exception {
            assertEquals(inputs.keySet(), values.keySet());
            for (Map.Entry<String, Long> value : values.entrySet()) inputs.get(value.getKey()).setValue(value.getValue());
            if (model != null) {
                model.doStep(false);
                assertTrue("Native model must settle successfully", model.isRunning());
            }
        }
    }

    @Test
    public void registersExposeDataAndClockWidthsOneThrough64AndCaptureOnlyRisingEdges() throws Exception {
        for (int bits = 1; bits <= 64; bits++) {
            JSONObject request = request("D_FF", new JSONObject().put("Bits", bits).put("Default", "0x0"), 0);
            assertEquals(Map.of("input:D", bits, "input:C", 1, "output:Q", bits, "output:~Q", bits),
                    widths(components.describe(request)));
            Harness register = bind(request);
            long mask = bits == 64 ? -1L : (1L << bits) - 1;
            register.evaluate(Map.of("D", mask, "C", 0L));
            assertEquals(0, register.outputs.get("Q").getValue());
            register.evaluate(Map.of("D", mask, "C", 1L));
            assertEquals(mask, register.outputs.get("Q").getValue());
            assertEquals(0, register.outputs.get("~Q").getValue());
            register.evaluate(Map.of("D", 0L, "C", 1L));
            assertEquals(mask, register.outputs.get("Q").getValue());
            register.evaluate(Map.of("D", 0L, "C", 0L));
            assertEquals(mask, register.outputs.get("Q").getValue());
            register.evaluate(Map.of("D", 0L, "C", 1L));
            assertEquals(0, register.outputs.get("Q").getValue());
            assertEquals(mask, register.outputs.get("~Q").getValue());
        }
    }

    private Harness bind(JSONObject request) throws Exception {
        VisualElement visual = components.create(request);
        JSONObject description = components.describe(visual, "test");
        Map<String, Integer> widths = widths(description);
        ElementAttributes attributes = visual.getElementAttributes();
        ElementTypeDescription type = components.library.getElementType(visual.getElementName());
        Element element = type.createElement(attributes);
        ObservableValues.Builder ordered = new ObservableValues.Builder();
        Map<String, ObservableValue> inputs = new HashMap<>();
        for (PinDescription pin : type.getInputDescription(attributes)) {
            ObservableValue value = new ObservableValue(pin.getName(), widths.get("input:" + pin.getName()));
            inputs.put(pin.getName(), value);
            ordered.add(value);
        }
        // Widths advertised by the adapter must be accepted by the native element.
        element.setInputs(ordered.build());
        Map<String, ObservableValue> outputs = new HashMap<>();
        for (ObservableValue output : element.getOutputs()) {
            assertEquals(widths.get("output:" + output.getName()).intValue(), output.getBits());
            outputs.put(output.getName(), output);
        }
        Model model = null;
        if (element instanceof Node node) {
            model = new Model();
            models.add(model);
            model.add(node);
            model.init(false);
            assertTrue("Native model must initialize successfully", model.isRunning());
        }
        return new Harness(element, model, inputs, outputs);
    }

    @Test
    public void busGateWidthsOneThrough64BindToNativeInputs() throws Exception {
        for (String type : new String[]{"And", "NAnd", "Or", "NOr", "XOr", "XNOr", "Not"}) {
            for (int bits = 1; bits <= 64; bits++) {
                JSONObject attributes = new JSONObject().put("Bits", bits);
                if (!type.equals("Not")) attributes.put("Inputs", 3);
                JSONObject request = request(type, attributes, 0);
                for (int width : widths(components.describe(request)).values()) assertEquals(bits, width);
                bind(request);
            }
        }
    }

    @Test
    public void addSubtractCarryPinsRemainOneBitAtEveryDataWidth() throws Exception {
        for (String type : new String[]{"Add", "Sub"}) {
            for (int bits = 1; bits <= 64; bits++) {
                JSONObject request = request(type, new JSONObject().put("Bits", bits), 0);
                assertEquals(Map.of("input:a", bits, "input:b", bits, "input:c_i", 1,
                        "output:s", bits, "output:c_o", 1), widths(components.describe(request)));
                Harness harness = bind(request);
                long mask = bits == 64 ? -1L : (1L << bits) - 1;
                for (long[] values : new long[][]{{0, 0, 0}, {0, 0, 1}, {mask, 0, 1}, {0, mask, 1}, {mask, mask, 1}}) {
                    harness.evaluate(Map.of("a", values[0], "b", values[1], "c_i", values[2]));
                    BigInteger a = new BigInteger(Long.toUnsignedString(values[0]));
                    BigInteger b = new BigInteger(Long.toUnsignedString(values[1]));
                    BigInteger carry = BigInteger.valueOf(values[2]);
                    BigInteger expected = type.equals("Add") ? a.add(b).add(carry) : a.subtract(b).subtract(carry);
                    assertEquals(type + " width " + bits, expected.longValue() & mask, harness.outputs.get("s").getValue());
                    long expectedCarry = type.equals("Add") ? (expected.bitLength() > bits ? 1 : 0) : (expected.signum() < 0 ? 1 : 0);
                    assertEquals(type + " carry/borrow width " + bits, expectedCarry, harness.outputs.get("c_o").getValue());
                }
            }
        }
    }

    @Test
    public void comparatorReportsScalarResultsAndHonorsSigned64BitOperands() throws Exception {
        for (int bits = 1; bits <= 64; bits++) {
            JSONObject request = request("Comparator", new JSONObject().put("Bits", bits), 0);
            assertEquals(Map.of("input:a", bits, "input:b", bits, "output:<", 1, "output:=", 1, "output:>", 1),
                    widths(components.describe(request)));
            bind(request);
        }
        for (boolean signed : new boolean[]{false, true}) {
            Harness harness = bind(request("Comparator", new JSONObject().put("Bits", 64).put("Signed", signed), 0));
            harness.evaluate(Map.of("a", Long.MIN_VALUE, "b", 0L));
            assertEquals(signed ? 1 : 0, harness.outputs.get("<").getValue());
            assertEquals(signed ? 0 : 1, harness.outputs.get(">").getValue());
            assertEquals(0, harness.outputs.get("=").getValue());
        }
    }

    @Test
    public void muxSelectionIsIndependentOfDataWidthAndPreservesTheSelectedWord() throws Exception {
        for (int bits = 1; bits <= 64; bits++) {
            int selectors = bits == 64 ? 8 : bits % 3 + 1;
            JSONObject request = request("Multiplexer", new JSONObject().put("Bits", bits).put("Selector Bits", selectors), 0);
            Map<String, Integer> pins = widths(components.describe(request));
            assertEquals(selectors, pins.get("input:sel").intValue());
            for (int index = 0; index < 1 << selectors; index++) assertEquals(bits, pins.get("input:in_" + index).intValue());
            Harness harness = bind(request);
            Map<String, Long> values = new HashMap<>();
            for (int index = 0; index < 1 << selectors; index++) values.put("in_" + index, (long) index);
            int last = (1 << selectors) - 1;
            values.put("in_" + last, -1L);
            values.put("sel", (long) last);
            harness.evaluate(values);
            assertEquals(bits == 64 ? -1L : (1L << bits) - 1, harness.outputs.get("out").getValue());
        }
    }

    @Test
    public void splitterBindingsIncludeSliceOffsetsAndAll64SingleBits() throws Exception {
        JSONObject request = request("Splitter", new JSONObject().put("Input Splitting", "64").put("Output Splitting", "1*64"), 0);
        JSONObject description = components.describe(request);
        JSONArray bindings = description.getJSONArray("splitter_bindings");
        assertEquals(65, bindings.length());
        assertEquals(64, widths(description).get("input:0-63").intValue());
        Harness harness = bind(request);
        long value = 0x8000000000000001L;
        harness.evaluate(Map.of("0-63", value));
        for (int bit = 0; bit < 64; bit++) {
            JSONObject binding = bindings.getJSONObject(bit + 1);
            assertEquals("output", binding.getString("direction"));
            assertEquals(bit, binding.getInt("offset"));
            assertEquals(1, binding.getInt("width"));
            assertEquals((value >>> bit) & 1, harness.outputs.get(binding.getString("pin")).getValue());
        }
        Harness reversed = bind(request("Splitter", new JSONObject().put("Input Splitting", "64")
                .put("Output Splitting", "63-60"), 0));
        reversed.evaluate(Map.of("0-63", 0xa000000000000000L));
        assertEquals(10, reversed.outputs.get("60-63").getValue());
    }

    @Test
    public void barrelShiftWidthsBindAndOversizedRightShiftsDoNotWrapAt64() throws Exception {
        for (int bits = 1; bits <= 64; bits++) {
            for (boolean signedAmount : new boolean[]{false, true}) {
                JSONObject attributes = new JSONObject().put("Bits", bits).put("barrelSigned", signedAmount);
                JSONObject request = request("BarrelShifter", attributes, 0);
                int expected = Integer.SIZE - Integer.numberOfLeadingZeros(bits) + (signedAmount ? 1 : 0);
                assertEquals(Map.of("input:in", bits, "input:shift", expected, "output:out", bits), widths(components.describe(request)));
                bind(request);
            }
        }
        for (int bits : new int[]{1, 3, 8, 32, 64}) {
            long negative = 1L << (bits - 1);
            for (String mode : new String[]{"logical", "arithmetic"}) {
                Harness harness = bind(request("BarrelShifter", new JSONObject().put("Bits", bits)
                        .put("barrelSigned", false).put("direction", "right").put("barrelShifterMode", mode), 0));
                for (long amount : new long[]{bits, (1L << harness.inputs.get("shift").getBits()) - 1}) {
                    harness.evaluate(Map.of("in", negative, "shift", amount));
                    long expected = mode.equals("logical") ? 0 : bits == 64 ? -1L : (1L << bits) - 1;
                    assertEquals(mode + " width " + bits + " amount " + amount, expected, harness.outputs.get("out").getValue());
                }
            }
        }
    }

    @Test
    public void rotatedBusPinsStayDistinctOnGridWithCardinalEscapes() throws Exception {
        for (String type : new String[]{"And", "Multiplexer", "Add", "Sub", "Comparator", "BarrelShifter", "Splitter"}) {
            JSONObject attributes = type.equals("Splitter")
                    ? new JSONObject().put("Input Splitting", "64").put("Output Splitting", "32,32")
                    : new JSONObject().put("Bits", 64);
            for (int rotation : new int[]{0, 90, 180, 270}) {
                JSONObject description = components.describe(request(type, attributes, rotation));
                HashSet<String> positions = new HashSet<>();
                for (Object object : description.getJSONArray("pins")) {
                    JSONObject pin = (JSONObject) object;
                    JSONObject position = pin.getJSONObject("position");
                    assertEquals(0, position.getInt("x") % 10);
                    assertEquals(0, position.getInt("y") % 10);
                    assertTrue(type, positions.add(position.getInt("x") + ":" + position.getInt("y")));
                    JSONObject escape = pin.getJSONObject("escape");
                    assertEquals(1, Math.abs(escape.getInt("x")) + Math.abs(escape.getInt("y")));
                }
            }
        }
    }
}
