package digitalagent;

import de.neemann.digital.draw.graphics.VectorFloat;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import javax.imageio.ImageIO;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.Assert.*;

public class PreviewMetadataTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder(new File("target").getAbsoluteFile());

    @Test public void exactFloatRendererTransformAndDimensionsSurviveClose() throws Exception {
        float scale = .37f;
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        OpaquePng png = new OpaquePng(bytes, scale);
        png.setBoundingBox(new VectorFloat(-13.25f, -27.75f), new VectorFloat(101.125f, 81.5f));
        png.close();
        JSONObject metadata = png.metadata(), transform = metadata.getJSONObject("circuit_to_pixel");
        assertEquals((double) scale, transform.getDouble("a"), 0);
        assertEquals((double) scale, transform.getDouble("d"), 0);
        assertEquals(17.25 * scale, transform.getDouble("tx"), 0);
        assertEquals(31.75 * scale, transform.getDouble("ty"), 0);
        assertEquals(0, transform.getDouble("b"), 0);
        assertEquals(0, transform.getDouble("c"), 0);
        var image = ImageIO.read(new java.io.ByteArrayInputStream(bytes.toByteArray()));
        assertEquals(image.getWidth(), metadata.getJSONObject("image").getInt("width_px"));
        assertEquals(image.getHeight(), metadata.getJSONObject("image").getInt("height_px"));
    }

    @Test public void nativeMetadataUsesCheckedIdsAndActualEncodedImage() throws Exception {
        DigitalAdapter adapter = new DigitalAdapter();
        Path path = temporary.getRoot().toPath().resolve("label.dig"), image = path.resolveSibling("label.png");
        JSONObject instance = new JSONObject().put("id", "original_id").put("type", "In")
                .put("attributes", new JSONObject().put("Label", "Işık Ω <plain>"))
                .put("position", new JSONObject().put("x", -100).put("y", -80)).put("rotation", 90);
        JSONObject emitted = adapter.dispatch("circuit.emit", new JSONObject().put("path", path.toString())
                .put("circuit", new JSONObject().put("schema_version", 1).put("instances", new JSONArray().put(instance)).put("wires", new JSONArray())));
        JSONObject request = new JSONObject().put("path", path.toString()).put("sidecar_path", emitted.getString("sidecar_path"))
                .put("format", "png").put("scale", .37).put("output_path", image.toString());
        JSONObject metadata = adapter.dispatch("circuit.render", request).getJSONObject("metadata");
        assertEquals(emitted.getString("sha256"), metadata.getJSONObject("circuit").getString("sha256"));
        assertEquals(emitted.getString("sidecar_sha256"), metadata.getJSONObject("circuit").getString("sidecar_sha256"));
        assertEquals(JsonSupport.sha256(Files.readAllBytes(image)), metadata.getJSONObject("image").getString("sha256"));
        JSONObject rendered = metadata.getJSONArray("instances").getJSONObject(0);
        assertEquals("original_id", rendered.getString("id"));
        assertEquals("Işık Ω <plain>", rendered.getString("label"));
        assertEquals(90, rendered.getInt("rotation"));
        assertFalse(rendered.getJSONArray("pins").getJSONObject(0).has("escape"));
        Path sidecar = Path.of(emitted.getString("sidecar_path"));
        JSONObject changed = new JSONObject(Files.readString(sidecar));
        changed.put("sha256", "0".repeat(64));
        Files.writeString(sidecar, changed.toString());
        try {
            adapter.dispatch("circuit.render", request);
            fail("Stale preview sidecar accepted");
        } catch (RpcException error) { assertEquals("STALE_SIDECAR", error.code); }
    }
}
