/*
 * Copyright (c) 2016 Helmut Neemann
 * Use of this source code is governed by the GPL v3 license
 * that can be found in the LICENSE file.
 */
package de.neemann.digital.draw.graphics;

import de.neemann.digital.core.element.Keys;
import de.neemann.digital.draw.elements.Circuit;
import de.neemann.digital.draw.elements.VisualElement;
import de.neemann.digital.draw.graphics.text.ParseException;
import de.neemann.digital.draw.graphics.text.Parser;
import de.neemann.digital.draw.library.ElementLibrary;
import de.neemann.digital.draw.shapes.ShapeFactory;
import junit.framework.TestCase;
import org.w3c.dom.Document;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;

/**
 */
public class GraphicSVGTest extends TestCase {
    public void testFormatText() throws Exception {
        GraphicSVG.TextStyle gs = new TextFormatSVG();

        assertEquals("Z0", gs.format("Z0", Style.NORMAL));
        assertEquals("&lt;a&gt;", gs.format("<a>", Style.NORMAL));
    }

    public void testMalformedLabelRemainsTextInActualSVGExport() throws Exception {
        String label = "broken_{<metadata>harmless & \"quoted\"</metadata>";
        try {
            new Parser(label).parse();
            fail("The regression label must exercise the parse-error fallback");
        } catch (ParseException expected) {
            // An unterminated formatting group must fall back to literal text.
        }

        ShapeFactory shapes = new ShapeFactory(new ElementLibrary(), true);
        Circuit circuit = new Circuit();
        circuit.add(new VisualElement("In").setShapeFactory(shapes).setPos(new Vector(0, 0))
                .setAttribute(Keys.LABEL, label));
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        new Export(circuit, stream -> new GraphicSVG(stream, null, 15)).export(output);

        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, "");
        Document document = factory.newDocumentBuilder().parse(new ByteArrayInputStream(output.toByteArray()));
        assertEquals(0, document.getElementsByTagNameNS("*", "metadata").getLength());
        assertTrue(document.getDocumentElement().getTextContent().contains(label));
    }

    public void testControlCharactersInLabelsKeepSVGWellFormed() throws Exception {
        // The first label parses as formatted text; the second takes the parse-error fallback.
        String[][] cases = {{"a\u0001b\u000Bc 🚀", "a�b�c 🚀"},
                {"broken_{\u0002 & <x>", "broken_{� & <x>"}};
        for (String[] item : cases) {
            ShapeFactory shapes = new ShapeFactory(new ElementLibrary(), true);
            Circuit circuit = new Circuit();
            circuit.add(new VisualElement("In").setShapeFactory(shapes).setPos(new Vector(0, 0))
                    .setAttribute(Keys.LABEL, item[0]));
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            new Export(circuit, stream -> new GraphicSVG(stream, null, 15)).export(output);

            DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
            factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, "");
            Document document = factory.newDocumentBuilder().parse(new ByteArrayInputStream(output.toByteArray()));
            assertTrue(item[0], document.getDocumentElement().getTextContent().contains(item[1]));
        }
        assertEquals("x�", new TextFormatSVG().format("x\uD800", Style.NORMAL));
    }

}
