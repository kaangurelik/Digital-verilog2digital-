package digitalagent;

import org.eclipse.elk.core.util.IElkProgressMonitor;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicLong;

import static org.junit.Assert.*;

/** Native fixed-port and malformed-net regressions for layout proposals. */
public class LayoutPlannerTest {
    private final DigitalAdapter adapter = new DigitalAdapter();

    private static JSONObject instance(String id, String type, int bits) {
        return new JSONObject().put("id", id).put("type", type)
                .put("attributes", new JSONObject().put("Bits", bits))
                .put("position", new JSONObject().put("x", 0).put("y", 0)).put("rotation", 0);
    }

    private static JSONObject endpoint(String id, String direction, String pin) {
        return new JSONObject().put("instance_id", id).put("direction", direction).put("pin", pin);
    }

    private static JSONObject net(String id, JSONObject... endpoints) {
        return new JSONObject().put("id", id).put("endpoints", new JSONArray(endpoints));
    }

    private static JSONObject simple() {
        return new JSONObject().put("instances", new JSONArray()
                        .put(instance("source", "In", 8)).put(instance("sink", "Out", 8)))
                .put("nets", new JSONArray().put(net("data", endpoint("source", "output", "out"), endpoint("sink", "input", "in"))));
    }

    private JSONObject plan(JSONObject request) throws Exception {
        String before = JsonSupport.canonical(request);
        JSONObject result = adapter.dispatch("layout.plan", request);
        assertEquals("Layout must not mutate caller electrical records", before, JsonSupport.canonical(request));
        assertTrue(result.getBoolean("proposal_only"));
        assertEquals("elk-layered-v1", result.getString("strategy"));
        assertEquals(1, result.getInt("seed"));
        assertEquals("0.12.0", result.getString("elk_version"));
        assertTrue(result.getJSONArray("diagnostics").isEmpty());
        checkNativePositions(request, result);
        for (Object item : result.getJSONArray("wires")) {
            JSONObject wire = (JSONObject) item;
            JSONObject a = wire.getJSONObject("from"), b = wire.getJSONObject("to");
            assertTrue(a.getInt("x") == b.getInt("x") || a.getInt("y") == b.getInt("y"));
            assertFalse(a.similar(b));
            assertEquals(0, a.getInt("x") % 10);
            assertEquals(0, a.getInt("y") % 10);
            assertEquals(0, b.getInt("x") % 10);
            assertEquals(0, b.getInt("y") % 10);
        }
        return result;
    }

    private void checkNativePositions(JSONObject request, JSONObject result) throws Exception {
        Map<String, JSONObject> positions = new HashMap<>(), envelopes = new HashMap<>(), specs = new HashMap<>();
        for (Object item : request.getJSONArray("instances")) {
            JSONObject value = (JSONObject) item;
            specs.put(value.getString("id"), value);
        }
        for (Object item : result.getJSONArray("placements")) {
            JSONObject value = (JSONObject) item;
            assertEquals(0, value.getJSONObject("position").getInt("x") % 20);
            assertEquals(0, value.getJSONObject("position").getInt("y") % 20);
            assertNull(positions.put(value.getString("id"), value));
            assertEquals(specs.get(value.getString("id")).getInt("rotation"), value.getInt("rotation"));
        }
        assertEquals(specs.keySet(), positions.keySet());
        for (Object item : result.getJSONArray("envelopes")) {
            JSONObject value = (JSONObject) item;
            envelopes.put(value.getString("id"), value.getJSONObject("bounds"));
        }
        Map<String, JSONObject> nativePins = new HashMap<>();
        for (Map.Entry<String, JSONObject> entry : specs.entrySet()) {
            JSONObject placed = new JSONObject(entry.getValue().toString())
                    .put("position", positions.get(entry.getKey()).getJSONObject("position"));
            JSONObject description = adapter.dispatch("component.describe", placed);
            for (Object pin : description.getJSONArray("pins")) {
                JSONObject value = (JSONObject) pin;
                nativePins.put(entry.getKey() + ":" + value.getString("direction") + ":" + value.getString("pin"), value);
            }
            JSONObject box = description.getJSONObject("bounds"), envelope = envelopes.get(entry.getKey());
            int spacing = request.optInt("spacing", 40);
            assertTrue(envelope.getJSONObject("min").getInt("x") <= box.getJSONObject("min").getInt("x") - spacing);
            assertTrue(envelope.getJSONObject("min").getInt("y") <= box.getJSONObject("min").getInt("y") - spacing);
            assertTrue(envelope.getJSONObject("max").getInt("x") >= box.getJSONObject("max").getInt("x") + spacing);
            assertTrue(envelope.getJSONObject("max").getInt("y") >= box.getJSONObject("max").getInt("y") + spacing);
        }
        Set<String> seen = new HashSet<>();
        for (Object item : result.getJSONArray("ports")) {
            JSONObject pin = (JSONObject) item;
            String key = pin.getString("instance_id") + ":" + pin.getString("direction") + ":" + pin.getString("pin");
            assertTrue(seen.add(key));
            JSONObject expected = nativePins.get(key);
            assertNotNull(expected);
            assertTrue(expected.getJSONObject("position").similar(pin.getJSONObject("position")));
            assertTrue(expected.getJSONObject("escape").similar(pin.getJSONObject("escape")));
            assertEquals(expected.getInt("width"), pin.getInt("width"));
            JSONObject position = pin.getJSONObject("position"), exit = pin.getJSONObject("exit"), escape = pin.getJSONObject("escape");
            int dx = exit.getInt("x") - position.getInt("x"), dy = exit.getInt("y") - position.getInt("y");
            assertEquals(dx * escape.getInt("y"), dy * escape.getInt("x"));
            assertTrue(dx * escape.getInt("x") + dy * escape.getInt("y") > 0);
            assertEquals(0, exit.getInt("x") % 10);
            assertEquals(0, exit.getInt("y") % 10);
        }
        assertEquals(nativePins.keySet(), seen);
    }

    private void reject(String code, JSONObject request) throws Exception {
        try {
            adapter.dispatch("layout.plan", request);
            fail("Malformed layout request accepted");
        } catch (RpcException error) {
            assertEquals(code, error.code);
        }
    }

    @Test
    public void positionsMatchDigitalForLongLabelsAndEveryCardinalRotation() throws Exception {
        for (int rotation = 0; rotation < 360; rotation += 90) {
            JSONObject request = simple();
            JSONObject source = request.getJSONArray("instances").getJSONObject(0);
            source.put("type", "Const").put("rotation", rotation);
            source.getJSONObject("attributes").put("Value", "0xff");
            request.getJSONArray("instances").getJSONObject(1).getJSONObject("attributes")
                    .put("Label", "long_native_label_123456789");
            JSONObject result = plan(request);
            assertEquals(2, result.getJSONArray("ports").length());
            assertFalse(result.getJSONArray("wires").isEmpty());
        }
    }

    @Test
    public void fanoutAndDisconnectedSingletonsAreDeterministicAcrossInputOrder() throws Exception {
        JSONObject request = simple();
        request.getJSONArray("instances").put(instance("sink2", "Out", 8)).put(instance("unused", "Const", 1));
        request.getJSONArray("nets").getJSONObject(0).getJSONArray("endpoints").put(endpoint("sink2", "input", "in"));
        request.getJSONArray("nets").put(net("unused_net", endpoint("unused", "output", "out")));
        JSONObject first = plan(request);
        assertEquals(JsonSupport.canonical(first), JsonSupport.canonical(plan(request)));
        JSONObject permuted = new JSONObject(request.toString());
        permuted.put("instances", reversed(permuted.getJSONArray("instances")));
        permuted.put("nets", reversed(permuted.getJSONArray("nets")));
        for (Object item : permuted.getJSONArray("nets")) {
            JSONObject net = (JSONObject) item;
            net.put("endpoints", reversed(net.getJSONArray("endpoints")));
        }
        assertEquals(JsonSupport.canonical(first), JsonSupport.canonical(plan(permuted)));
        for (Object item : first.getJSONArray("wires")) assertEquals("data", ((JSONObject) item).getString("net_id"));
    }

    private static JSONArray reversed(JSONArray array) {
        JSONArray result = new JSONArray();
        for (int i = array.length() - 1; i >= 0; i--) result.put(array.get(i));
        return result;
    }

    @Test
    public void multiplexerKeepsItsSouthSelectorAndExactBusPorts() throws Exception {
        JSONObject request = new JSONObject().put("instances", new JSONArray()
                        .put(instance("data", "In", 8)).put(instance("select", "In", 1))
                        .put(instance("mux", "Multiplexer", 8)).put(instance("sink", "Out", 8)))
                .put("nets", new JSONArray()
                        .put(net("data_net", endpoint("data", "output", "out"), endpoint("mux", "input", "in_0"), endpoint("mux", "input", "in_1")))
                        .put(net("select_net", endpoint("select", "output", "out"), endpoint("mux", "input", "sel")))
                        .put(net("result_net", endpoint("mux", "output", "out"), endpoint("sink", "input", "in"))));
        JSONObject result = plan(request);
        JSONObject selector = null;
        for (Object item : result.getJSONArray("ports")) {
            JSONObject pin = (JSONObject) item;
            if (pin.getString("pin").equals("sel")) selector = pin;
        }
        assertNotNull(selector);
        assertEquals(0, selector.getJSONObject("escape").getInt("x"));
        assertEquals(1, selector.getJSONObject("escape").getInt("y"));
    }

    @Test
    public void complete64BitSplitterFanoutPreservesAllNativePinIdentities() throws Exception {
        JSONObject splitter = instance("split", "Splitter", 64).put("attributes", new JSONObject()
                .put("Input Splitting", "64").put("Output Splitting", "1*64"));
        JSONArray instances = new JSONArray().put(instance("source", "In", 64)).put(splitter);
        JSONArray nets = new JSONArray().put(net("bus", endpoint("source", "output", "out"), endpoint("split", "input", "0-63")));
        for (int bit = 0; bit < 64; bit++) {
            String id = "out_" + bit;
            instances.put(instance(id, "Out", 1));
            nets.put(net("bit_" + bit, endpoint("split", "output", Integer.toString(bit)), endpoint(id, "input", "in")));
        }
        JSONObject result = plan(new JSONObject().put("instances", instances).put("nets", nets));
        assertEquals(66, result.getJSONArray("placements").length());
        assertEquals(130, result.getJSONArray("ports").length());
    }

    @Test
    public void accepts250DisconnectedNativeComponentsWithoutInventingWires() throws Exception {
        JSONArray instances = new JSONArray(), nets = new JSONArray();
        for (int i = 0; i < 250; i++) {
            instances.put(instance("c" + i, "Const", 1));
            nets.put(net("n" + i, endpoint("c" + i, "output", "out")));
        }
        JSONObject result = plan(new JSONObject().put("instances", instances).put("nets", nets));
        assertEquals(250, result.getJSONArray("placements").length());
        assertTrue(result.getJSONArray("wires").isEmpty());
        JSONArray boxes = result.getJSONArray("envelopes");
        for (int i = 0; i < boxes.length(); i++) {
            JSONObject a = boxes.getJSONObject(i).getJSONObject("bounds");
            for (int j = i + 1; j < boxes.length(); j++) {
                JSONObject b = boxes.getJSONObject(j).getJSONObject("bounds");
                assertTrue(a.getJSONObject("max").getInt("x") <= b.getJSONObject("min").getInt("x")
                        || b.getJSONObject("max").getInt("x") <= a.getJSONObject("min").getInt("x")
                        || a.getJSONObject("max").getInt("y") <= b.getJSONObject("min").getInt("y")
                        || b.getJSONObject("max").getInt("y") <= a.getJSONObject("min").getInt("y"));
            }
        }
    }

    @Test
    public void rejectsAmbiguousOwnershipWidthsLimitsAndOptions() throws Exception {
        reject("UNKNOWN_FIELD", simple().put("algorithm", "arbitrary"));
        reject("INTEGER_RANGE", simple().put("seed", 0));
        reject("INVALID_SPACING", simple().put("spacing", 41));
        reject("EMPTY_LAYOUT", new JSONObject().put("instances", new JSONArray()).put("nets", new JSONArray()));
        JSONObject request = simple();
        request.getJSONArray("instances").getJSONObject(0).getJSONObject("position").put("x", 20);
        reject("LAYOUT_ORIGIN_REQUIRED", request);
        request = simple();
        request.getJSONArray("instances").put(request.getJSONArray("instances").getJSONObject(0));
        reject("DUPLICATE_INSTANCE", request);
        request = simple();
        request.getJSONArray("nets").getJSONObject(0).getJSONArray("endpoints").put(endpoint("source", "output", "out"));
        reject("DUPLICATE_ENDPOINT", request);
        request = simple();
        request.getJSONArray("nets").getJSONObject(0).getJSONArray("endpoints").getJSONObject(0).put("pin", "invented");
        reject("UNKNOWN_ENDPOINT", request);
        request = simple();
        request.getJSONArray("nets").getJSONObject(0).getJSONArray("endpoints").remove(1);
        reject("MISSING_ENDPOINT", request);
        request = simple();
        request.getJSONArray("instances").getJSONObject(1).getJSONObject("attributes").put("Bits", 1);
        reject("NET_WIDTH_MISMATCH", request);
        request = simple();
        request.getJSONArray("nets").getJSONObject(0).getJSONArray("endpoints").remove(0);
        reject("INVALID_NET_DRIVER", request);
        request = simple();
        for (int i = 0; i < 249; i++) request.getJSONArray("instances").put(instance("c" + i, "Const", 1));
        reject("RESOURCE_LIMIT", request);
    }

    @Test
    public void registerFeedbackRemainsAnElectricalNetDuringLayeredPlacement() throws Exception {
        JSONObject request = new JSONObject().put("instances", new JSONArray()
                        .put(instance("clock", "In", 1)).put(instance("ff", "D_FF", 8)).put(instance("sink", "Out", 8)))
                .put("nets", new JSONArray()
                        .put(net("clock_net", endpoint("clock", "output", "out"), endpoint("ff", "input", "C")))
                        .put(net("feedback_net", endpoint("ff", "output", "Q"), endpoint("ff", "input", "D"), endpoint("sink", "input", "in")))
                        .put(net("unused_complement", endpoint("ff", "output", "~Q"))));
        JSONObject result = plan(request);
        assertEquals(3, result.getJSONArray("placements").length());
        assertTrue(result.getJSONArray("ports").length() >= 6);
        for (Object value : result.getJSONArray("wires")) {
            assertTrue(Set.of("clock_net", "feedback_net").contains(((JSONObject) value).getString("net_id")));
        }
    }

    @Test
    public void deadlinePropagatesIntoNestedElkMonitorsWithoutSleeping() throws Exception {
        AtomicLong now = new AtomicLong(10);
        LayoutPlanner.DeadlineMonitor monitor = new LayoutPlanner.DeadlineMonitor(20, now::get);
        monitor.begin("layout", 1);
        IElkProgressMonitor child = monitor.subTask(1);
        child.begin("child", 1);
        IElkProgressMonitor grandchild = child.subTask(1);
        assertFalse(monitor.isCanceled());
        assertFalse(grandchild.isCanceled());
        now.set(20);
        assertTrue(monitor.isCanceled());
        assertTrue(child.isCanceled());
        assertTrue(grandchild.isCanceled());
        try {
            new LayoutPlanner(new Components(), () -> 0L, 0).plan(simple());
            fail("Expired layout was executed");
        } catch (RpcException error) {
            assertEquals("LAYOUT_TIMEOUT", error.code);
        }
    }
}
