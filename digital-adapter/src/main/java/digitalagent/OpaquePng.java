package digitalagent;

import de.neemann.digital.draw.graphics.Graphic;
import de.neemann.digital.draw.graphics.GraphicSwing;
import de.neemann.digital.draw.graphics.Style;
import de.neemann.digital.draw.graphics.VectorInterface;

import javax.imageio.ImageIO;
import java.awt.Color;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.io.OutputStream;
import org.json.JSONObject;

/** Native Digital drawing on one opaque white buffer, suitable for any preview viewer. */
final class OpaquePng extends GraphicSwing {
    private final OutputStream output;
    private final float scale;
    private BufferedImage image;
    private Graphics2D graphics;
    private double[] transform;

    OpaquePng(OutputStream output, float scale) {
        super(null);
        this.output = output;
        this.scale = scale;
    }

    @Override
    public Graphic setBoundingBox(VectorInterface min, VectorInterface max) {
        int thickness = Style.MAXLINETHICK;
        int width = Math.round((max.getXFloat() - min.getXFloat() + thickness * 2) * scale);
        int height = Math.round((max.getYFloat() - min.getYFloat() + thickness * 2) * scale);
        // Repeat the limit at allocation using the exact float arithmetic that determines dimensions.
        if (width < 1 || height < 1 || width > 16384 || height > 16384 || (long) width * height > 16_000_000) {
            throw new IllegalArgumentException("PNG dimensions exceed the bounded preview budget");
        }
        image = new BufferedImage(width, height, BufferedImage.TYPE_INT_RGB);
        graphics = image.createGraphics();
        graphics.setColor(Color.WHITE);
        graphics.fillRect(0, 0, width, height);
        graphics.setRenderingHint(RenderingHints.KEY_RENDERING, RenderingHints.VALUE_RENDER_QUALITY);
        graphics.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
        graphics.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_ON);
        graphics.setRenderingHint(RenderingHints.KEY_FRACTIONALMETRICS, RenderingHints.VALUE_FRACTIONALMETRICS_ON);
        graphics.scale(scale, scale);
        graphics.translate(thickness - min.getXFloat(), thickness - min.getYFloat());
        transform = new double[6];
        graphics.getTransform().getMatrix(transform);
        setGraphics2D(graphics);
        return this;
    }

    JSONObject metadata() {
        if (image == null || transform == null) throw new IllegalStateException("PNG has not been rendered");
        return new JSONObject().put("image", new JSONObject().put("width_px", image.getWidth()).put("height_px", image.getHeight()))
                .put("circuit_to_pixel", new JSONObject().put("a", transform[0]).put("b", transform[1])
                        .put("c", transform[2]).put("d", transform[3]).put("tx", transform[4]).put("ty", transform[5]));
    }

    @Override
    public void close() throws IOException {
        try {
            if (graphics != null) graphics.dispose();
            if (image != null && !ImageIO.write(image, "png", output)) throw new IOException("PNG encoder unavailable");
        } finally {
            output.close();
        }
    }
}
