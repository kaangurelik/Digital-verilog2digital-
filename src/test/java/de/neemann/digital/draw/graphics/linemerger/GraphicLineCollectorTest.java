/*
 * Copyright (c) 2018 Helmut Neemann
 * Use of this source code is governed by the GPL v3 license
 * that can be found in the LICENSE file.
 */
package de.neemann.digital.draw.graphics.linemerger;

import de.neemann.digital.draw.graphics.*;
import junit.framework.TestCase;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.awt.Color;
import java.awt.Graphics2D;
import java.awt.image.BufferedImage;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.util.ArrayList;

public class GraphicLineCollectorTest extends TestCase {

    public void testOpenSquare() {
        GraphicLineCollector col = new GraphicLineCollector();
        col.drawLine(new Vector(0, 0), new Vector(10, 0), Style.NORMAL);
        col.drawLine(new Vector(0, 10), new Vector(10, 10), Style.NORMAL);

        col.drawLine(new Vector(0, 0), new Vector(0, 10), Style.NORMAL);

        ArrayList<Polygon> poly = new ArrayList<>();
        col.drawTo(new MyGraphic(poly));

        assertEquals(1, poly.size());
        assertEquals("M 10,0 L 0,0 L 0,10 L 10,10", poly.get(0).toString());
    }

    public void testClosedSquare() {
        GraphicLineCollector col = new GraphicLineCollector();
        col.drawLine(new Vector(0, 0), new Vector(10, 0), Style.NORMAL);
        col.drawLine(new Vector(0, 10), new Vector(10, 10), Style.NORMAL);

        col.drawLine(new Vector(0, 0), new Vector(0, 10), Style.NORMAL);
        col.drawLine(new Vector(10, 0), new Vector(10, 10), Style.NORMAL);

        ArrayList<Polygon> poly = new ArrayList<>();
        col.drawTo(new MyGraphic(poly));

        assertEquals(1, poly.size());
        assertEquals("M 10,0 L 0,0 L 0,10 L 10,10 Z", poly.get(0).toString());
    }

    public void testClosedSquare2() {
        GraphicLineCollector col = new GraphicLineCollector();
        col.drawLine(new Vector(0, 0), new Vector(10, 0), Style.NORMAL);
        col.drawLine(new Vector(0, 10), new Vector(10, 10), Style.NORMAL);

        col.drawLine(new Vector(0, 0), new Vector(0, 10), Style.NORMAL);
        col.drawLine(new Vector(10, 0), new Vector(10, 10), Style.NORMAL);

        ArrayList<Polygon> poly = new ArrayList<>();
        col.drawTo(new MyGraphic(poly));

        assertEquals(1, poly.size());
        assertEquals("M 10,0 L 0,0 L 0,10 L 10,10 Z", poly.get(0).toString());
    }

    public void testStyleGroupsRetainFirstEncounterOrderWhileMerging() {
        for (Style[] styles : styleOrders()) {
            GraphicLineCollector collector = layeredLines(styles);
            // A disconnected line stays behind the first style's merged line.
            collector.drawLine(new Vector(30, 20), new Vector(40, 20), styles[0]);
            ArrayList<Polygon> polygons = new ArrayList<>();
            ArrayList<Style> drawnStyles = new ArrayList<>();
            collector.drawTo(new MyGraphic(polygons) {
                @Override
                public void drawPolygon(Polygon polygon, Style style) {
                    super.drawPolygon(polygon, style);
                    drawnStyles.add(style);
                }
            });

            assertEquals(4, polygons.size());
            assertSame(styles[0], drawnStyles.get(0));
            assertSame(styles[0], drawnStyles.get(1));
            assertSame(styles[1], drawnStyles.get(2));
            assertSame(styles[2], drawnStyles.get(3));
            assertEquals("M 0,10 L 10,10 L 20,10", polygons.get(0).toString());
            assertEquals("M 30,20 L 40,20", polygons.get(1).toString());
        }
    }

    public void testSvgLayersFollowStyleEncounterOrder() throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, "");
        for (Style[] styles : styleOrders()) {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            try (GraphicSVG svg = new GraphicSVG(output, null, 15)) {
                svg.setBoundingBox(new Vector(0, 0), new Vector(20, 20));
                layeredLines(styles).drawTo(svg);
            }
            NodeList paths = factory.newDocumentBuilder()
                    .parse(new ByteArrayInputStream(output.toByteArray()))
                    .getElementsByTagNameNS("*", "path");
            assertEquals(styles.length, paths.getLength());
            for (int index = 0; index < styles.length; index++) {
                Element path = (Element) paths.item(index);
                assertEquals("M 0,10 L 10,10 L 20,10", path.getAttribute("d"));
                assertEquals("#" + Integer.toHexString(styles[index].getColor().getRGB()).substring(2),
                        path.getAttribute("stroke"));
            }
        }
    }

    public void testRasterOverlapUsesLastEncounteredStyleGroup() {
        for (Style[] styles : styleOrders()) {
            BufferedImage image = new BufferedImage(24, 24, BufferedImage.TYPE_INT_ARGB);
            Graphics2D graphics = image.createGraphics();
            try {
                GraphicSwing raster = new GraphicSwing(graphics);
                raster.enableAntiAlias(false);
                layeredLines(styles).drawTo(raster);
            } finally {
                graphics.dispose();
            }
            assertEquals("The same SVG paint order must determine the visible raster color",
                    styles[styles.length - 1].getColor().getRGB(), image.getRGB(10, 10));
        }
    }

    private static GraphicLineCollector layeredLines(Style[] styles) {
        GraphicLineCollector collector = new GraphicLineCollector();
        for (Style style : styles)
            collector.drawLine(new Vector(0, 10), new Vector(10, 10), style);
        // Encountering an existing style again must merge its line without
        // changing the established group order (including the first style).
        for (int index = styles.length - 1; index >= 0; index--)
            collector.drawLine(new Vector(10, 10), new Vector(20, 10), styles[index]);
        return collector;
    }

    private static Style[][] styleOrders() {
        Style black = Style.NORMAL.deriveColor(Color.BLACK);
        Style blue = Style.WIRE_LOW.deriveColor(Color.BLUE);
        Style red = Style.WIRE_HIGH.deriveColor(Color.RED);
        // Reversals exercise paint order independently of identity hash order.
        return new Style[][]{
                {black, blue, red}, {black, red, blue},
                {blue, black, red}, {blue, red, black},
                {red, black, blue}, {red, blue, black}
        };
    }

    private static class MyGraphic extends Graphic {
        private final ArrayList<Polygon> poly;

        private MyGraphic(ArrayList<Polygon> poly) {
            this.poly = poly;
        }

        @Override
        public void drawLine(VectorInterface p1, VectorInterface p2, Style style) {
        }

        @Override
        public void drawPolygon(Polygon p, Style style) {
            poly.add(p);
        }

        @Override
        public void drawCircle(VectorInterface p1, VectorInterface p2, Style style) {
        }

        @Override
        public void drawText(VectorInterface p1, VectorInterface p2, VectorInterface p3, String text, Orientation orientation, Style style) {
        }
    }
}
