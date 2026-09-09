package digitalagent;

import de.neemann.digital.draw.elements.Circuit;
import de.neemann.digital.draw.elements.VisualElement;
import org.json.JSONArray;
import org.json.JSONObject;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;
import org.xml.sax.InputSource;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.StringReader;
import java.util.ArrayList;
import java.util.Comparator;

/** Stable signatures include every serialized field, independent of HashMap entry order. */
final class SerializedIdentity {
    private SerializedIdentity() { }

    static String signature(VisualElement element) throws Exception {
        String xml = Circuit.getxStream().toXML(element);
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        factory.setFeature("http://xml.org/sax/features/external-general-entities", false);
        factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, "");
        Element root = factory.newDocumentBuilder().parse(new InputSource(new StringReader(xml))).getDocumentElement();
        return JsonSupport.sha256(JsonSupport.canonical(canonicalElement(root)));
    }

    private static JSONObject canonicalElement(Element element) {
        JSONObject attributes = new JSONObject();
        for (int i = 0; i < element.getAttributes().getLength(); i++) {
            Node attribute = element.getAttributes().item(i);
            attributes.put(attribute.getNodeName(), attribute.getNodeValue());
        }
        ArrayList<JSONObject> children = new ArrayList<>();
        StringBuilder text = new StringBuilder();
        boolean mapEntries = true;
        NodeList nodes = element.getChildNodes();
        for (int i = 0; i < nodes.getLength(); i++) {
            Node child = nodes.item(i);
            if (child instanceof Element childElement) {
                children.add(canonicalElement(childElement));
                mapEntries &= childElement.getTagName().equals("entry");
            } else if (child.getNodeType() == Node.TEXT_NODE || child.getNodeType() == Node.CDATA_SECTION_NODE) {
                text.append(child.getNodeValue());
            }
        }
        if (mapEntries) children.sort(Comparator.comparing(JsonSupport::canonical));
        return new JSONObject().put("tag", element.getTagName()).put("attributes", attributes)
                .put("children", new JSONArray(children)).put("text", children.isEmpty() ? text.toString() : "");
    }
}
