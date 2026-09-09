package digitalagent;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.Assert.*;

/** Regression checks for native metadata and trustworthy file-to-instance association. */
public class DigitalAdapterTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder(new File("target").getAbsoluteFile());

    private final DigitalAdapter adapter = new DigitalAdapter();

    private JSONObject describe(String type, JSONObject attributes) throws Exception {
        return adapter.dispatch("component.describe", new JSONObject().put("type", type).put("attributes", attributes));
    }

    private void reject(String code, String type, JSONObject attributes) throws Exception {
        try {
            describe(type, attributes);
            fail("Invalid component accepted");
        } catch (RpcException error) {
            assertEquals(code, error.code);
        }
    }

    private JSONObject instance(String id, String type, int x, JSONObject attributes) {
        return new JSONObject().put("id", id).put("type", type).put("attributes", attributes)
                .put("position", new JSONObject().put("x", x).put("y", 0)).put("rotation", 0);
    }

    private JSONObject emit() throws Exception {
        JSONObject circuit = new JSONObject().put("schema_version", 1)
                .put("instances", new JSONArray().put(instance("source", "In", 0, new JSONObject().put("Label", "same")))
                        .put(instance("sink", "Out", 100, new JSONObject().put("Label", "same"))))
                .put("wires", new JSONArray().put(new JSONObject().put("from", new JSONObject().put("x", 0).put("y", 0))
                        .put("to", new JSONObject().put("x", 100).put("y", 0))));
        return adapter.dispatch("circuit.emit", new JSONObject().put("circuit", circuit)
                .put("path", temporary.getRoot().toPath().resolve("test.dig").toString()));
    }

    private JSONObject inspect(JSONObject emitted) throws Exception {
        return adapter.dispatch("circuit.inspect", new JSONObject().put("path", emitted.getString("path"))
                .put("sidecar_path", emitted.getString("sidecar_path")));
    }

    @Test
    public void rejectsUnsafeCoercionsAndNativeKeyBounds() throws Exception {
        reject("INVALID_INTEGER", "And", new JSONObject().put("Bits", true));
        reject("INVALID_INTEGER", "And", new JSONObject().put("Bits", 1.5));
        reject("INTEGER_RANGE", "And", new JSONObject().put("Bits", 65));
        reject("INTEGER_RANGE", "Multiplexer", new JSONObject().put("Selector Bits", 9));
        reject("UNKNOWN_ATTRIBUTE", "And", new JSONObject().put("Bit", 1));
        reject("UNSUPPORTED_ATTRIBUTE", "In", new JSONObject().put("InDefault", "0x0"));
        reject("ROTATION_FIELD_REQUIRED", "And", new JSONObject().put("rotation", 90));
        reject("RESOURCE_LIMIT", "And", new JSONObject().put("Inputs", Integer.MAX_VALUE));
        reject("RESOURCE_LIMIT", "Splitter", new JSONObject().put("Output Splitting", "1*2147483647"));
        reject("DUPLICATE_PIN", "Splitter", new JSONObject().put("Output Splitting", "1,0-0"));
    }

    @Test
    public void unsignedConstantsPreserveAll64BitsAndNativeOutputWidth() throws Exception {
        JSONObject description = describe("Const", new JSONObject().put("Bits", 64).put("Value", "0xffffffffffffffff"));
        assertEquals("0xffffffffffffffff", description.getJSONObject("attributes").getString("Value"));
        assertEquals(64, description.getJSONArray("pins").getJSONObject(0).getInt("width"));
    }

    @Test
    public void constantEscapeFacesAwayFromItsValueAtEveryNativeRotation() throws Exception {
        int[][] expected = {{1, 0}, {0, -1}, {-1, 0}, {0, 1}};
        for (int turn = 0; turn < expected.length; turn++) {
            JSONObject description = adapter.dispatch("component.describe", new JSONObject()
                    .put("type", "Const").put("attributes", new JSONObject().put("Bits", 1).put("Value", "0x0"))
                    .put("position", new JSONObject().put("x", 100).put("y", -200)).put("rotation", turn * 90));
            JSONArray pins = description.getJSONArray("pins");
            assertEquals(1, pins.length());
            JSONObject pin = pins.getJSONObject(0);
            assertEquals("output", pin.getString("direction"));
            assertEquals("out", pin.getString("pin"));
            assertEquals(100, pin.getJSONObject("position").getInt("x"));
            assertEquals(-200, pin.getJSONObject("position").getInt("y"));
            assertEquals(expected[turn][0], pin.getJSONObject("escape").getInt("x"));
            assertEquals(expected[turn][1], pin.getJSONObject("escape").getInt("y"));
        }
    }

    @Test
    public void splitterExportsNativeOffsetsWithoutConflatingDirections() throws Exception {
        JSONObject description = describe("Splitter", new JSONObject().put("Input Splitting", "8").put("Output Splitting", "7-0"));
        JSONArray bindings = description.getJSONArray("splitter_bindings");
        assertEquals(2, bindings.length());
        assertEquals("0-7", bindings.getJSONObject(0).getString("pin"));
        assertEquals("0-7", bindings.getJSONObject(1).getString("pin"));
        assertEquals("input", bindings.getJSONObject(0).getString("direction"));
        assertEquals("output", bindings.getJSONObject(1).getString("direction"));
        assertEquals(0, bindings.getJSONObject(1).getInt("offset"));
        assertEquals(8, bindings.getJSONObject(1).getInt("width"));
    }

    @Test
    public void duplicateLabelsDoNotChangeInstanceAssociationOrSimulation() throws Exception {
        JSONObject emitted = emit();
        JSONObject report = inspect(emitted);
        assertTrue(report.getJSONObject("model").getBoolean("valid"));
        assertTrue(report.getBoolean("topology_available"));
        assertEquals("source", report.getJSONArray("instances").getJSONObject(0).getString("id"));
        assertEquals("sink", report.getJSONArray("instances").getJSONObject(1).getString("id"));
        assertEquals(2, report.getJSONArray("nets").getJSONObject(0).getJSONArray("endpoints").length());
        JSONObject result = adapter.dispatch("circuit.simulate", new JSONObject().put("path", emitted.getString("path"))
                .put("sidecar_path", emitted.getString("sidecar_path"))
                .put("stimuli", new JSONArray().put(new JSONObject().put("values", new JSONObject().put("source", "0x1")))));
        assertEquals("0x1", result.getJSONArray("samples").getJSONObject(0).getJSONObject("values").getString("sink"));
    }

    @Test
    public void fileChangesInvalidateTheSidecar() throws Exception {
        JSONObject emitted = emit();
        Path path = Path.of(emitted.getString("path"));
        Files.writeString(path, Files.readString(path) + "\n");
        try {
            inspect(emitted);
            fail("Changed file reused an old sidecar");
        } catch (RpcException error) {
            assertEquals("STALE_SIDECAR", error.code);
        }
    }

    @Test
    public void componentSignaturesStillRejectChangesWhenFileHashIsUpdated() throws Exception {
        JSONObject emitted = emit();
        Path path = Path.of(emitted.getString("path"));
        String content = Files.readString(path, StandardCharsets.UTF_8).replace("<string>same</string>", "<string>changed</string>");
        Files.writeString(path, content, StandardCharsets.UTF_8);
        Path sidecarPath = Path.of(emitted.getString("sidecar_path"));
        JSONObject sidecar = new JSONObject(Files.readString(sidecarPath));
        sidecar.put("sha256", JsonSupport.sha256(Files.readAllBytes(path)));
        Files.writeString(sidecarPath, sidecar.toString());
        try {
            inspect(emitted);
            fail("Changed serialized component passed its old signature");
        } catch (RpcException error) {
            assertEquals("INSTANCE_ASSOCIATION", error.code);
        }
    }

    @Test
    public void svgMetadataIsDeterministicAndPngAllocationIsBounded() throws Exception {
        JSONObject emitted = emit();
        Path first = temporary.getRoot().toPath().resolve("a.svg");
        Path second = temporary.getRoot().toPath().resolve("b.svg");
        JSONObject render = new JSONObject().put("path", emitted.getString("path")).put("format", "svg");
        adapter.dispatch("circuit.render", render.put("output_path", first.toString()));
        adapter.dispatch("circuit.render", render.put("output_path", second.toString()));
        assertArrayEquals(Files.readAllBytes(first), Files.readAllBytes(second));
        assertFalse(Files.readString(first).contains("<!-- created:"));
        Path png = temporary.getRoot().toPath().resolve("preview.png");
        adapter.dispatch("circuit.render", render.put("format", "png").put("output_path", png.toString()));
        BufferedImage preview = ImageIO.read(png.toFile());
        assertEquals(0xffffffff, preview.getRGB(0, 0));
        assertEquals(0xffffffff, preview.getRGB(preview.getWidth() - 1, preview.getHeight() - 1));
        boolean hasDarkStroke = false;
        for (int y = 0; y < preview.getHeight(); y++) {
            for (int x = 0; x < preview.getWidth(); x++) {
                int pixel = preview.getRGB(x, y);
                assertEquals("Preview must remain opaque", 255, pixel >>> 24);
                if ((pixel & 0x00ffffff) == 0) hasDarkStroke = true;
            }
        }
        assertTrue("Native black component strokes must remain visible", hasDarkStroke);
        try {
            adapter.dispatch("circuit.render", render.put("format", "png").put("scale", 100));
            fail("Unbounded scale accepted");
        } catch (RpcException error) {
            assertEquals("INVALID_SCALE", error.code);
        }
    }
}
