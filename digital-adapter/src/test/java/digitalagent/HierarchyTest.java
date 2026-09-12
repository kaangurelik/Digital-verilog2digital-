package digitalagent;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import de.neemann.digital.draw.elements.Circuit;
import de.neemann.digital.draw.elements.VisualElement;

import java.io.File;
import java.io.ByteArrayOutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

import static org.junit.Assert.*;

/** Real native nested files, custom pins, alias merges and independent state occurrences. */
public class HierarchyTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder(new File("target").getAbsoluteFile());
    private final DigitalAdapter adapter = new DigitalAdapter();
    private final JSONArray modules = new JSONArray();
    private int serial;

    private JSONObject context() {
        return new JSONObject().put("root", temporary.getRoot().getAbsolutePath()).put("modules", modules);
    }

    private JSONObject call(String method, JSONObject params) throws Exception {
        return adapter.dispatch(method, new JSONObject(params.toString()).put("package", context()));
    }

    private static JSONObject point(int x, int y) { return new JSONObject().put("x", x).put("y", y); }

    private static JSONObject endpoint(String id, String direction, String pin) {
        return new JSONObject().put("instance_id", id).put("direction", direction).put("pin", pin);
    }

    private static JSONObject row(Object... pairs) {
        JSONObject values = new JSONObject();
        for (int i = 0; i < pairs.length; i += 2) values.put((String) pairs[i], pairs[i + 1]);
        return new JSONObject().put("values", values);
    }

    private final class Fixture {
        final Map<String, JSONObject> specs = new LinkedHashMap<>();
        final Map<String, JSONObject[]> nets = new LinkedHashMap<>();
        Fixture add(String id, String type, int bits, String label) {
            JSONObject attrs = new JSONObject().put("Label", label);
            if (!type.endsWith(".dig")) attrs.put("Bits", bits);
            specs.put(id, new JSONObject().put("id", id).put("type", type).put("attributes", attrs)
                    .put("position", point(specs.size() * 800, 0)).put("rotation", 0));
            return this;
        }
        Fixture net(String name, JSONObject... pins) { nets.put(name, pins); return this; }
        JSONObject emit(String type, String label) throws Exception {
            JSONArray placed = new JSONArray(specs.values()), wires = new JSONArray();
            int tunnel = 0;
            for (Map.Entry<String, JSONObject[]> net : nets.entrySet()) {
                for (JSONObject end : net.getValue()) {
                    JSONObject description = call("component.describe", specs.get(end.getString("instance_id")));
                    JSONObject pin = null;
                    for (Object value : description.getJSONArray("pins")) {
                        JSONObject item = (JSONObject) value;
                        if (item.getString("direction").equals(end.getString("direction"))
                                && item.getString("pin").equals(end.getString("pin"))) pin = item;
                    }
                    assertNotNull(end.toString(), pin);
                    JSONObject p = pin.getJSONObject("position"), escape = pin.getJSONObject("escape");
                    JSONObject exit = point(p.getInt("x") + 60 * escape.getInt("x"), p.getInt("y") + 60 * escape.getInt("y"));
                    int rotation = escape.getInt("x") == 1 ? 0 : escape.getInt("x") == -1 ? 180 : escape.getInt("y") == -1 ? 90 : 270;
                    placed.put(new JSONObject().put("id", "t" + tunnel++).put("type", "Tunnel")
                            .put("attributes", new JSONObject().put("NetName", net.getKey())).put("position", exit).put("rotation", rotation));
                    wires.put(new JSONObject().put("from", p).put("to", exit));
                }
            }
            return call("circuit.emit", new JSONObject().put("path", temporary.getRoot().toPath().resolve(type).toString())
                    .put("circuit", new JSONObject().put("schema_version", 1).put("label", label).put("instances", placed).put("wires", wires)));
        }
        String register(String label) throws Exception {
            String type = "m_" + String.format("%024x", ++serial) + ".dig";
            JSONObject result = emit(type, label);
            modules.put(new JSONObject().put("type", type).put("sha256", result.getString("sha256"))
                    .put("sidecar_sha256", result.getString("sidecar_sha256")));
            return type;
        }
    }

    private Fixture alias(int bits) {
        return new Fixture().add("a", "In", bits, "a").add("y", "Out", bits, "y")
                .net("same", endpoint("a", "output", "out"), endpoint("y", "input", "in"));
    }

    private Fixture register(int bits) {
        return new Fixture().add("clk", "In", 1, "clk").add("d", "In", bits, "d")
                .add("ff", "D_FF", bits, "state").add("q", "Out", bits, "q")
                .net("clock", endpoint("clk", "output", "out"), endpoint("ff", "input", "C"))
                .net("data", endpoint("d", "output", "out"), endpoint("ff", "input", "D"))
                .net("output", endpoint("ff", "output", "Q"), endpoint("q", "input", "in"));
    }

    private static JSONObject paths(JSONObject emitted) {
        return new JSONObject().put("path", emitted.getString("path")).put("sidecar_path", emitted.getString("sidecar_path"));
    }

    private static Set<String> endpointIds(JSONObject report) {
        TreeSet<String> ids = new TreeSet<>();
        for (Object group : report.getJSONArray("nets")) for (Object value : ((JSONObject) group).getJSONArray("endpoints")) {
            ids.add(((JSONObject) value).getString("instance_id"));
        }
        return ids;
    }

    @Test public void physicalCustomPortsSurviveWhileNativeExpansionMergesAliasedParentNets() throws Exception {
        String child = alias(64).register("Readable 64-bit alias");
        Fixture parent = new Fixture().add("a", "In", 64, "a").add("u0", child, 0, "first")
                .add("y", "Out", 64, "y")
                .net("left", endpoint("a", "output", "out"), endpoint("u0", "input", "a"))
                .net("right", endpoint("u0", "output", "y"), endpoint("y", "input", "in"));
        JSONObject output = parent.emit("circuit.dig", "Parent");
        JSONObject inspection = call("circuit.inspect", paths(output));
        assertTrue(inspection.toString(), inspection.getJSONObject("model").getBoolean("valid"));
        assertTrue(endpointIds(inspection).contains("u0"));
        JSONObject expanded = inspection.getJSONObject("expanded");
        assertFalse(endpointIds(expanded).contains("u0"));
        assertFalse(endpointIds(expanded).contains("u0.a"));
        boolean merged = false;
        for (Object value : expanded.getJSONArray("nets")) {
            Set<String> ids = endpointIds(new JSONObject().put("nets", new JSONArray().put(value)));
            if (ids.contains("a") && ids.contains("y")) merged = true;
        }
        assertTrue("Native alias must merge separate parent nets", merged);
        JSONObject simulation = call("circuit.simulate", paths(output).put("stimuli", new JSONArray()
                .put(row("a", "0x8000000000000001"))));
        assertEquals("0x8000000000000001", simulation.getJSONArray("samples").getJSONObject(0).getJSONObject("values").getString("y"));
        JSONObject described = call("component.describe", parent.specs.get("u0"));
        for (Object value : described.getJSONArray("pins")) assertEquals(64, ((JSONObject) value).getInt("width"));
    }

    @Test public void repeatedNestedRegistersHaveDistinctScopedStateAndLocalTunnelNames() throws Exception {
        String leaf = register(64).register("Register W=64");
        String middle = new Fixture().add("clk", "In", 1, "clk").add("d", "In", 64, "d")
                .add("n0", leaf, 0, "nested").add("q", "Out", 64, "q")
                .net("clock", endpoint("clk", "output", "out"), endpoint("n0", "input", "clk"))
                .net("data", endpoint("d", "output", "out"), endpoint("n0", "input", "d"))
                .net("output", endpoint("n0", "output", "q"), endpoint("q", "input", "in")).register("Nested register");
        Fixture parent = new Fixture().add("clk", "In", 1, "clk").add("d0", "In", 64, "d0").add("d1", "In", 64, "d1")
                .add("u0", middle, 0, "same display").add("u1", middle, 0, "same display")
                .add("q0", "Out", 64, "q0").add("q1", "Out", 64, "q1")
                .net("clock", endpoint("clk", "output", "out"), endpoint("u0", "input", "clk"), endpoint("u1", "input", "clk"))
                .net("data0", endpoint("d0", "output", "out"), endpoint("u0", "input", "d"))
                .net("data1", endpoint("d1", "output", "out"), endpoint("u1", "input", "d"))
                .net("output0", endpoint("u0", "output", "q"), endpoint("q0", "input", "in"))
                .net("output1", endpoint("u1", "output", "q"), endpoint("q1", "input", "in"));
        JSONObject output = parent.emit("circuit.dig", "Repeated nested state");
        JSONObject inspected = call("circuit.inspect", paths(output));
        assertTrue(inspected.toString(), inspected.getJSONObject("model").getBoolean("valid"));
        assertTrue(endpointIds(inspected.getJSONObject("expanded")).containsAll(Set.of("u0.n0.ff", "u1.n0.ff")));
        JSONObject result = call("circuit.simulate_cycles", paths(output).put("clock_instance_id", "clk")
                .put("initialization", new JSONObject().put("kind", "input_sequence").put("cycles", new JSONArray()
                        .put(row("d0", "0x8000000000000000", "d1", "0x1"))))
                .put("cycles", new JSONArray().put(row("d0", "0x3", "d1", "0xffffffffffffffff"))));
        JSONObject defaults = result.getJSONObject("assumptions").getJSONObject("initialization").getJSONObject("native_defaults");
        assertEquals(Set.of("u0.n0.ff", "u1.n0.ff"), defaults.keySet());
        JSONArray samples = result.getJSONArray("samples");
        assertEquals("0x8000000000000000", samples.getJSONObject(0).getJSONObject("values").getString("q0"));
        assertEquals("0x1", samples.getJSONObject(0).getJSONObject("values").getString("q1"));
        assertEquals("0x3", samples.getJSONObject(1).getJSONObject("values").getString("q0"));
        assertEquals("0xffffffffffffffff", samples.getJSONObject(1).getJSONObject("values").getString("q1"));
    }

    @Test public void registryRejectsChangedCircuitAndSidecarBeforeAnyOperation() throws Exception {
        String type = alias(1).register("Alias");
        Path file = temporary.getRoot().toPath().resolve(type);
        byte[] original = Files.readAllBytes(file);
        Files.writeString(file, "changed");
        RpcException circuit = assertThrows(RpcException.class, () -> call("component.describe", new JSONObject().put("type", type)));
        assertEquals("PACKAGE_HASH", circuit.code);
        Files.write(file, original);
        Files.writeString(file.resolveSibling(type + ".ids.json"), "changed");
        RpcException sidecar = assertThrows(RpcException.class, () -> call("component.describe", new JSONObject().put("type", type)));
        assertEquals("PACKAGE_HASH", sidecar.code);
    }

    @Test public void registryRejectsExternalBasenamesAndUnregisteredChildren() throws Exception {
        JSONObject bad = context();
        bad.getJSONArray("modules").put(new JSONObject().put("type", "../outside.dig").put("sha256", "0".repeat(64)).put("sidecar_sha256", "0".repeat(64)));
        RpcException escaped = assertThrows(RpcException.class, () -> adapter.dispatch("component.describe", new JSONObject().put("type", "In").put("package", bad)));
        assertEquals("PACKAGE_TYPE", escaped.code);
        modules.clear();
        RpcException missing = assertThrows(RpcException.class, () -> call("component.describe", new JSONObject().put("type", "m_" + "a".repeat(24) + ".dig")));
        assertEquals("UNSUPPORTED_COMPONENT", missing.code);
    }

    @Test public void packageRelocationUsesOnlyNewRootAndFreshContext() throws Exception {
        String type = alias(8).register("Same display name");
        JSONObject descriptor = call("component.describe", new JSONObject().put("type", type));
        Path moved = temporary.newFolder("relocated").toPath();
        for (String suffix : new String[]{"", ".ids.json"}) Files.move(temporary.getRoot().toPath().resolve(type + suffix), moved.resolve(type + suffix));
        JSONObject relocated = new JSONObject().put("root", moved.toString()).put("modules", modules);
        JSONObject after = adapter.dispatch("component.describe", new JSONObject().put("type", type).put("package", relocated));
        assertEquals(JsonSupport.canonical(descriptor), JsonSupport.canonical(after));
        Path svg = moved.resolve("preview.svg");
        adapter.dispatch("circuit.render", new JSONObject().put("path", moved.resolve(type).toString()).put("package", relocated)
                .put("format", "svg").put("output_path", svg.toString()));
        assertTrue(Files.size(svg) > 100);
    }

    private void rawModule(String type, String child, String id) throws Exception {
        Circuit circuit = new Circuit();
        if (child != null) circuit.add(new VisualElement(child));
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        circuit.save(output);
        byte[] bytes = output.toByteArray();
        JSONArray associations = new JSONArray();
        for (int i = 0; i < circuit.getElements().size(); i++) {
            associations.put(new JSONObject().put("id", id).put("index", i)
                    .put("signature", SerializedIdentity.signature(circuit.getElements().get(i))));
        }
        JSONObject sidecar = new JSONObject().put("schema_version", 1).put("sha256", JsonSupport.sha256(bytes)).put("instances", associations);
        byte[] sidecarBytes = JsonSupport.canonical(sidecar).getBytes(java.nio.charset.StandardCharsets.UTF_8);
        Files.write(temporary.getRoot().toPath().resolve(type), bytes);
        Files.write(temporary.getRoot().toPath().resolve(type + ".ids.json"), sidecarBytes);
        modules.put(new JSONObject().put("type", type).put("sha256", JsonSupport.sha256(bytes)).put("sidecar_sha256", JsonSupport.sha256(sidecarBytes)));
    }

    @Test public void packageRejectsRecursiveAndDeepGraphsBeforeNativeExpansion() throws Exception {
        String a = "m_" + "a".repeat(24) + ".dig", b = "m_" + "b".repeat(24) + ".dig";
        rawModule(a, b, "child");
        rawModule(b, a, "child");
        RpcException recursive = assertThrows(RpcException.class, () -> call("component.describe", new JSONObject().put("type", a)));
        assertEquals("PACKAGE_CYCLE", recursive.code);
        modules.clear();
        String previous = null;
        for (int i = 0; i <= 13; i++) {
            String type = "m_" + String.format("%024x", i) + ".dig";
            rawModule(type, previous, "child");
            previous = type;
        }
        RpcException depth = assertThrows(RpcException.class, () -> call("component.describe", new JSONObject().put("type", a)));
        assertEquals("PACKAGE_DEPTH", depth.code);
    }

    @Test public void packageRejectsAmbiguousLocalIdentityEvenWithValidArtifactHashes() throws Exception {
        String type = "m_" + "c".repeat(24) + ".dig";
        rawModule(type, "In", "u0.child");
        RpcException identity = assertThrows(RpcException.class, () -> call("component.describe", new JSONObject().put("type", type)));
        assertEquals("INSTANCE_ASSOCIATION", identity.code);
    }

    @Test public void repeatedCallsDoNotReuseDescriptionsFromEarlierHashInventory() throws Exception {
        String type = alias(8).register("Same module name");
        JSONObject before = call("component.describe", new JSONObject().put("type", type));
        modules.clear();
        JSONObject emitted = alias(16).emit(type, "Same module name");
        modules.put(new JSONObject().put("type", type).put("sha256", emitted.getString("sha256"))
                .put("sidecar_sha256", emitted.getString("sidecar_sha256")));
        JSONObject after = call("component.describe", new JSONObject().put("type", type));
        assertEquals(8, before.getJSONArray("pins").getJSONObject(0).getInt("width"));
        assertEquals(16, after.getJSONArray("pins").getJSONObject(0).getInt("width"));
    }

    @Test public void previewCannotOverwriteAnotherRegisteredArtifact() throws Exception {
        String first = alias(1).register("First"), second = alias(1).register("Second");
        Path target = temporary.getRoot().toPath().resolve(second);
        byte[] before = Files.readAllBytes(target);
        RpcException immutable = assertThrows(RpcException.class, () -> call("circuit.render", new JSONObject()
                .put("path", temporary.getRoot().toPath().resolve(first).toString())
                .put("format", "svg").put("output_path", target.toString())));
        assertEquals("PACKAGE_IMMUTABLE", immutable.code);
        assertArrayEquals(before, Files.readAllBytes(target));
    }
}
