package digitalagent;

import de.neemann.digital.core.Bits;
import de.neemann.digital.core.ObservableValue;
import de.neemann.digital.core.element.ElementAttributes;
import de.neemann.digital.core.element.ElementTypeDescription;
import de.neemann.digital.core.element.Key;
import de.neemann.digital.core.element.Keys;
import de.neemann.digital.core.element.PinDescription;
import de.neemann.digital.core.wiring.Splitter;
import de.neemann.digital.draw.elements.Pin;
import de.neemann.digital.draw.elements.VisualElement;
import de.neemann.digital.draw.graphics.GraphicMinMax;
import de.neemann.digital.draw.graphics.TransformRotate;
import de.neemann.digital.draw.graphics.Vector;
import de.neemann.digital.draw.library.ElementLibrary;
import de.neemann.digital.draw.library.ElementTypeDescriptionCustom;
import de.neemann.digital.draw.shapes.ShapeFactory;
import org.json.JSONArray;
import org.json.JSONObject;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

import static digitalagent.JsonSupport.*;

/** Native component metadata with explicitly supported attribute codecs. */
final class Components {
    static final int COORDINATE_LIMIT = 1_000_000;
    static final int MAX_PINS = 1024;
    static final Set<String> SUPPORTED = Set.of("In", "Out", "Const", "And", "NAnd", "Or", "NOr",
            "XOr", "XNOr", "Not", "Multiplexer", "Add", "Sub", "Comparator", "BarrelShifter", "Splitter", "Tunnel", "D_FF");
    final ElementLibrary library;
    final ShapeFactory shapes;
    private final PackageContext context;

    Components() { this(null); }

    Components(PackageContext context) {
        this.context = context;
        library = context == null ? new ElementLibrary() : context.library;
        shapes = new ShapeFactory(library, true);
    }

    JSONObject catalog(JSONObject params) throws Exception {
        fields(params);
        ArrayList<ElementTypeDescription> descriptions = new ArrayList<>();
        for (ElementLibrary.ElementContainer item : library) descriptions.add(item.getDescription());
        descriptions.sort(Comparator.comparing(ElementTypeDescription::getName));
        JSONArray result = new JSONArray();
        for (ElementTypeDescription description : descriptions) {
            JSONArray attributes = new JSONArray();
            for (Key<?> key : description.getAttributeList()) {
                JSONObject entry = new JSONObject().put("name", key.getKey()).put("codec", codec(key));
                if (!codec(key).equals("unsupported") && key != Keys.ROTATE) {
                    entry.put("default", encode(key.getDefault()));
                }
                if (key instanceof Key.KeyInteger integer) {
                    entry.put("minimum", integer.getMin()).put("maximum", integer.getMax());
                }
                if (key.getValueClass().isEnum()) {
                    JSONArray values = new JSONArray();
                    for (Object value : key.getValueClass().getEnumConstants()) values.put(((Enum<?>) value).name());
                    entry.put("values", values);
                }
                attributes.put(entry);
            }
            result.put(new JSONObject().put("type", description.getName())
                    .put("emittable", SUPPORTED.contains(description.getName())).put("attributes", attributes));
        }
        return new JSONObject().put("schema_version", 1).put("components", result);
    }

    VisualElement create(JSONObject request) throws Exception {
        fields(request, "id", "type", "attributes", "position", "rotation");
        String type = string(request, "type");
        checkType(type);
        ElementTypeDescription description = library.getElementType(type);
        JSONObject attributes = optionalObject(request, "attributes");
        VisualElement element = new VisualElement(type).setShapeFactory(shapes);
        for (String name : attributes.keySet()) {
            Key<?> found = null;
            for (Key<?> key : description.getAttributeList()) if (key.getKey().equals(name)) found = key;
            if (found == null) throw invalid("UNKNOWN_ATTRIBUTE", type + " has no supported attribute " + name);
            if (found == Keys.ROTATE) throw invalid("ROTATION_FIELD_REQUIRED", "Use the rotation field in degrees");
            setAttribute(element.getElementAttributes(), found, attributes.get(name));
        }
        int rotation = (int) integer(request.has("rotation") ? request.get("rotation") : 0,
                "rotation", 0, 270);
        if (rotation % 90 != 0) throw invalid("INVALID_ROTATION", "rotation must be 0, 90, 180 or 270 degrees");
        if (rotation != 0 && !description.hasAttribute(Keys.ROTATE)) {
            throw invalid("UNSUPPORTED_ROTATION", type + " does not support rotation");
        }
        element.setRotation(rotation / 90);
        element.setPos(request.has("position") ? position(object(request, "position")) : new Vector(0, 0));
        validateResourceBounds(element);
        return element;
    }

    void checkType(String type) throws RpcException {
        if (!SUPPORTED.contains(type) && (context == null || !context.contains(type))) {
            throw invalid("UNSUPPORTED_COMPONENT", "Component is not in the adapter inventory or package: " + type);
        }
    }

    void validateLoaded(VisualElement element) throws Exception {
        checkType(element.getElementName());
        ElementTypeDescription description = library.getElementType(element.getElementName());
        ElementAttributes attributes = element.getElementAttributes();
        for (Key<?> key : description.getAttributeList()) {
            Object value = attributes.get(key);
            if (!key.getValueClass().isInstance(value)) {
                throw invalid("INVALID_ATTRIBUTE_TYPE", "Reloaded attribute has the wrong type: " + key.getKey());
            }
            if (key == Keys.ROTATE) continue;
            if (codec(key).equals("unsupported")) {
                if (attributes.contains(key) && !value.equals(key.getDefault())) {
                    throw invalid("UNSUPPORTED_ATTRIBUTE", "Reloaded component uses unsupported attribute " + key.getKey());
                }
            } else setAttribute(new ElementAttributes(), key, encode(value));
        }
        // ModelEntry inverts configured inputs of every type, including types that do not list the key.
        if (!description.hasAttribute(Keys.INVERTER_CONFIG) && attributes.contains(Keys.INVERTER_CONFIG)
                && !Keys.INVERTER_CONFIG.getDefault().equals(attributes.get(Keys.INVERTER_CONFIG))) {
            throw invalid("UNSUPPORTED_ATTRIBUTE", "Reloaded component uses unsupported attribute " + Keys.INVERTER_CONFIG.getKey());
        }
        integer(element.getRotate(), "rotation quarter-turns", 0, 3);
        position(point(element.getPos()));
        validateResourceBounds(element);
    }

    private void validateResourceBounds(VisualElement element) throws Exception {
        ElementAttributes attributes = element.getElementAttributes();
        // Bound pin counts before allocating native FanIn arrays.
        if (attributes.get(Keys.INPUT_COUNT) > MAX_PINS) throw invalid("RESOURCE_LIMIT", "Maximum inputs is " + MAX_PINS);
        if (element.getElementName().equals("Splitter")) {
            for (Key<String> key : new Key[]{Keys.INPUT_SPLIT, Keys.OUTPUT_SPLIT}) {
                String definition = attributes.get(key);
                if (definition.length() > 2048) throw invalid("RESOURCE_LIMIT", "Splitter definition is too long");
                // Validate counts before the native grammar allocates repeated ports.
                long requestedPorts = 0;
                for (String field : definition.split(",")) {
                    int star = field.indexOf('*');
                    if (star >= 0) {
                        try {
                            int count = Integer.decode(field.substring(star + 1).trim());
                            if (count < 1 || count > MAX_PINS) throw invalid("RESOURCE_LIMIT", "Splitter repetition is out of range");
                            requestedPorts += count;
                        } catch (NumberFormatException error) {
                            throw invalid("INVALID_SPLITTER", "Invalid splitter repetition");
                        }
                    } else requestedPorts++;
                    if (requestedPorts > MAX_PINS) throw invalid("RESOURCE_LIMIT", "Too many splitter ports");
                }
                Splitter.Ports ports = new Splitter.Ports(definition);
                int count = 0;
                for (Splitter.Port port : ports) {
                    if (++count > MAX_PINS || port.getBits() < 1 || port.getBits() > 64 || port.getPos() < 0
                            || (long) port.getPos() + port.getBits() > 64) {
                        throw invalid("INVALID_SPLITTER", "Splitter ports must be within bits 0 through 63");
                    }
                }
            }
        }
        if (element.getPins().size() > MAX_PINS) throw invalid("RESOURCE_LIMIT", "Component has too many pins");
        HashSet<String> names = new HashSet<>();
        for (Pin pin : element.getPins()) {
            if (!names.add(pin.getDirection().name() + ":" + pin.getName())) {
                throw invalid("DUPLICATE_PIN", "Component repeats a pin name within one direction: " + pin.getName());
            }
        }
    }

    JSONObject describe(JSONObject params) throws Exception {
        return describe(create(params), null);
    }

    JSONObject describe(VisualElement element, String id) throws Exception {
        checkType(element.getElementName());
        ElementTypeDescription description = library.getElementType(element.getElementName());
        ElementAttributes attributes = element.getElementAttributes();
        JSONObject resolved = new JSONObject();
        JSONArray unsupported = new JSONArray();
        for (Key<?> key : description.getAttributeList()) {
            if (key == Keys.ROTATE) continue;
            if (codec(key).equals("unsupported")) unsupported.put(key.getKey());
            else resolved.put(key.getKey(), encode(attributes.get(key)));
        }
        JSONObject body = bounds(element.getMinMax(false));
        Map<String, Integer> outputWidths = new HashMap<>();
        if (description instanceof ElementTypeDescriptionCustom custom) {
            // Custom output placeholders throw from getBits until the child model exists.
            for (VisualElement output : custom.getCircuit().getElements()) {
                if (output.getElementName().equals("Out")) {
                    outputWidths.put(output.getElementAttributes().getLabel(), output.getElementAttributes().getBits());
                }
            }
        } else {
            for (ObservableValue value : description.createElement(attributes).getOutputs()) outputWidths.put(value.getName(), value.getBits());
        }
        JSONArray pins = new JSONArray();
        for (Pin pin : element.getPins()) {
            Integer width = pin.getDirection() == PinDescription.Direction.input
                    ? inputWidth(element, pin.getName()) : outputWidths.get(pin.getName());
            pins.put(new JSONObject().put("direction", pin.getDirection().name()).put("pin", pin.getName())
                    .put("position", point(pin.getPos())).put("width", width == null ? JSONObject.NULL : width)
                    .put("escape", element.getElementName().equals("Const")
                            // Const draws its value left of a point-sized body; route away from the label.
                            ? point(new TransformRotate(new Vector(0, 0), element.getRotate()).transform(new Vector(1, 0)))
                            : escape(pin.getPos(), body)));
        }
        JSONObject result = new JSONObject().put("schema_version", 1).put("type", element.getElementName())
                .put("attributes", resolved).put("unsupported_attributes", unsupported)
                .put("position", point(element.getPos())).put("rotation", element.getRotate() * 90)
                .put("pins", pins).put("bounds", bounds(element.getMinMax(true))).put("body_bounds", body);
        if (id != null) result.put("id", id);
        if (element.getElementName().equals("Splitter")) {
            JSONArray bindings = new JSONArray();
            appendBindings(bindings, attributes.get(Keys.INPUT_SPLIT), "input");
            appendBindings(bindings, attributes.get(Keys.OUTPUT_SPLIT), "output");
            result.put("splitter_bindings", bindings);
        }
        return result;
    }

    private void appendBindings(JSONArray target, String definition, String direction) throws Exception {
        for (Splitter.Port port : new Splitter.Ports(definition)) {
            target.put(new JSONObject().put("direction", direction).put("pin", port.getName())
                    .put("offset", port.getPos()).put("width", port.getBits()));
        }
    }

    private Integer inputWidth(VisualElement element, String name) throws Exception {
        ElementAttributes attributes = element.getElementAttributes();
        ElementTypeDescription description = library.getElementType(element.getElementName());
        if (description instanceof ElementTypeDescriptionCustom custom) {
            for (VisualElement input : custom.getCircuit().getElements()) {
                if (input.getElementName().equals("In") && input.getElementAttributes().getLabel().equals(name)) {
                    return input.getElementAttributes().getBits();
                }
            }
            throw invalid("CUSTOM_PIN_WIDTH", "Custom input has no corresponding native In: " + name);
        }
        return switch (element.getElementName()) {
            case "Out", "And", "NAnd", "Or", "NOr", "XOr", "XNOr", "Not", "Comparator" -> attributes.getBits();
            case "Add", "Sub" -> name.equals("c_i") ? 1 : attributes.getBits();
            case "Multiplexer" -> name.equals("sel") ? attributes.get(Keys.SELECTOR_BITS) : attributes.getBits();
            // Shift-count width must represent the full data width, e.g. 8 needs 4 bits.
            case "BarrelShifter" -> name.equals("shift")
                    ? Bits.binLn2(attributes.getBits()) + (attributes.get(Keys.BARREL_SIGNED) ? 1 : 0)
                    : attributes.getBits();
            case "D_FF" -> name.equals("C") ? 1 : attributes.getBits();
            case "Splitter" -> {
                Integer width = null;
                for (Splitter.Port port : new Splitter.Ports(attributes.get(Keys.INPUT_SPLIT))) {
                    if (port.getName().equals(name)) width = port.getBits();
                }
                yield width;
            }
            default -> null;
        };
    }

    private static JSONObject escape(Vector pin, JSONObject bounds) {
        JSONObject min = bounds.getJSONObject("min");
        JSONObject max = bounds.getJSONObject("max");
        int[] distances = {Math.abs(pin.x - min.getInt("x")), Math.abs(pin.x - max.getInt("x")),
                Math.abs(pin.y - min.getInt("y")), Math.abs(pin.y - max.getInt("y"))};
        int selected = 0;
        for (int i = 1; i < distances.length; i++) if (distances[i] < distances[selected]) selected = i;
        return new JSONObject().put("x", selected == 0 ? -1 : selected == 1 ? 1 : 0)
                .put("y", selected == 2 ? -1 : selected == 3 ? 1 : 0);
    }

    static JSONObject point(Vector vector) {
        return new JSONObject().put("x", vector.x).put("y", vector.y);
    }

    static Vector position(JSONObject value) throws RpcException {
        fields(value, "x", "y");
        Vector result = new Vector((int) integer(value.opt("x"), "x", -COORDINATE_LIMIT, COORDINATE_LIMIT),
                (int) integer(value.opt("y"), "y", -COORDINATE_LIMIT, COORDINATE_LIMIT));
        if (result.x % 10 != 0 || result.y % 10 != 0) throw invalid("OFF_GRID", "Coordinates must be multiples of 10");
        return result;
    }

    static JSONObject bounds(GraphicMinMax value) throws RpcException {
        if (!value.isValid()) throw invalid("INVALID_GEOMETRY", "Component has no drawable bounds");
        return new JSONObject().put("min", new JSONObject().put("x", (int) Math.floor(value.getMin().getXFloat()))
                        .put("y", (int) Math.floor(value.getMin().getYFloat())))
                .put("max", new JSONObject().put("x", (int) Math.ceil(value.getMax().getXFloat()))
                        .put("y", (int) Math.ceil(value.getMax().getYFloat())));
    }

    private static String codec(Key<?> key) {
        if (key == Keys.ROTATE) return "rotation_degrees";
        Class<?> type = key.getValueClass();
        if (type == Integer.class) return "integer";
        if (type == Long.class) return "uint64_hex";
        if (type == Boolean.class) return "boolean";
        if (type == String.class) return "string";
        if (type.isEnum()) return "enum";
        return "unsupported";
    }

    static Object encode(Object value) {
        if (value instanceof Long integer) return "0x" + Long.toUnsignedString(integer, 16);
        if (value instanceof Enum<?> enumeration) return enumeration.name();
        return value;
    }

    @SuppressWarnings({"rawtypes", "unchecked"})
    private static void setAttribute(ElementAttributes attributes, Key key, Object value) throws RpcException {
        Class<?> type = key.getValueClass();
        Object converted;
        if (type == Integer.class) {
            long min = key instanceof Key.KeyInteger integer ? integer.getMin() : Integer.MIN_VALUE;
            long max = key instanceof Key.KeyInteger integer ? integer.getMax() : Integer.MAX_VALUE;
            converted = (int) integer(value, key.getKey(), min, max);
        } else if (type == Long.class) {
            if (value instanceof String text && text.matches("0[xX][0-9a-fA-F]{1,16}")) {
                converted = new BigInteger(text.substring(2), 16).longValue();
            } else converted = integer(value, key.getKey(), Long.MIN_VALUE, Long.MAX_VALUE);
        } else if (type == Boolean.class) {
            if (!(value instanceof Boolean)) throw invalid("INVALID_ATTRIBUTE_TYPE", key.getKey() + " requires boolean");
            converted = value;
        } else if (type == String.class) {
            if (!(value instanceof String text) || text.length() > 16384) {
                throw invalid("INVALID_ATTRIBUTE_TYPE", key.getKey() + " requires a string of at most 16384 characters");
            }
            converted = value;
        } else if (type.isEnum()) {
            if (!(value instanceof String)) throw invalid("INVALID_ATTRIBUTE_TYPE", key.getKey() + " requires an enum name");
            try {
                converted = Enum.valueOf(type.asSubclass(Enum.class), (String) value);
            } catch (IllegalArgumentException error) {
                throw invalid("INVALID_ATTRIBUTE_VALUE", "Unknown value for " + key.getKey() + ": " + value);
            }
        } else throw invalid("UNSUPPORTED_ATTRIBUTE", "No supported codec for " + key.getKey());
        attributes.set(key, converted);
    }
}
