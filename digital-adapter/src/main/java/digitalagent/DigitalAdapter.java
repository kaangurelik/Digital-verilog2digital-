package digitalagent;

import de.neemann.digital.core.Model;
import de.neemann.digital.core.ModelEventType;
import de.neemann.digital.core.ObservableValue;
import de.neemann.digital.draw.elements.Circuit;
import de.neemann.digital.draw.elements.Pin;
import de.neemann.digital.draw.elements.VisualElement;
import de.neemann.digital.draw.elements.Wire;
import de.neemann.digital.draw.graphics.Export;
import de.neemann.digital.draw.graphics.GraphicMinMax;
import de.neemann.digital.draw.graphics.GraphicSVG;
import de.neemann.digital.draw.graphics.Style;
import de.neemann.digital.draw.graphics.Vector;
import de.neemann.digital.draw.model.ModelCreator;
import de.neemann.digital.draw.model.ModelEntry;
import de.neemann.digital.draw.model.Net;
import de.neemann.digital.draw.model.NetList;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

import static digitalagent.JsonSupport.*;

/** Digital is the sole source of the reloaded circuit's component geometry and topology. */
public final class DigitalAdapter {
    private static final long MAX_ARTIFACT_BYTES = 64L * 1024 * 1024;
    private static final int MAX_INSTANCES = 1024;
    private static final int MAX_WIRES = 20000;
    private static final long MAX_PNG_PIXELS = 16_000_000;
    private static final int MAX_PNG_DIMENSION = 16384;
    private final Components components;
    private final PackageContext context;

    public DigitalAdapter() { this(null); }

    private DigitalAdapter(PackageContext context) {
        this.context = context;
        components = context == null ? new Components() : context.components;
    }

    public JSONObject dispatch(String method, JSONObject params) throws Exception {
        try {
            if (params.has("package")) {
                if (context != null || method.equals("capabilities") || method.equals("component.catalog")) {
                    throw invalid("PACKAGE_METHOD", "Package context is not supported for this method");
                }
                PackageContext requestContext = new PackageContext(object(params, "package"));
                JSONObject operation = new JSONObject(params.toString());
                operation.remove("package");
                JSONObject result = new DigitalAdapter(requestContext).dispatch(method, operation);
                requestContext.recheck();
                return result;
            }
            return switch (method) {
                case "capabilities" -> capabilities(params);
                case "component.catalog" -> components.catalog(params);
                case "component.describe" -> components.describe(params);
                case "layout.plan" -> new LayoutPlanner(components).plan(params);
                case "circuit.emit" -> emit(params);
                case "circuit.inspect" -> inspect(params);
                case "circuit.render" -> render(params);
                case "circuit.simulate" -> simulate(params);
                case "circuit.simulate_cycles" -> simulateCycles(params);
                default -> throw new RpcException(-32601, "METHOD_NOT_FOUND", "Unknown method: " + method);
            };
        } catch (RpcException error) {
            throw error;
        } catch (Exception error) {
            error.printStackTrace(System.err);
            throw new RpcException(-32000, "DIGITAL_OPERATION_FAILED", detail(error));
        }
    }

    private JSONObject capabilities(JSONObject params) throws RpcException {
        fields(params);
        return new JSONObject().put("schema_version", 1).put("adapter_version", "0.1.0")
                .put("digital_revision", "ad6d68fe2e0277cad02beaa7f2a87e366a971b39")
                .put("shape_style", "ieee").put("grid", 10).put("placement_grid", 20).put("wire_grid", 10)
                .put("methods", new JSONArray(List.of("capabilities", "component.catalog", "component.describe",
                        "layout.plan", "circuit.emit", "circuit.inspect", "circuit.render", "circuit.simulate", "circuit.simulate_cycles")))
                .put("supported_types", new JSONArray(new TreeSet<>(Components.SUPPORTED)))
                .put("limits", new JSONObject().put("instances", MAX_INSTANCES).put("wires", MAX_WIRES)
                        .put("coordinate", Components.COORDINATE_LIMIT).put("png_pixels", MAX_PNG_PIXELS)
                        .put("layout_instances", LayoutPlanner.MAX_INSTANCES).put("layout_endpoints", LayoutPlanner.MAX_ENDPOINTS)
                        .put("layout_timeout_ms", LayoutPlanner.TIMEOUT_NANOS / 1_000_000)
                        .put("prefix_cycles", CycleSimulator.MAX_PREFIX_CYCLES).put("observed_cycles", CycleSimulator.MAX_CYCLES)
                        .put("cycle_response_bytes", CycleSimulator.MAX_RESPONSE_BYTES)
                        .put("cycle_timeout_ms", CycleSimulator.TIMEOUT_NANOS / 1_000_000))
                .put("attribute_codecs", new JSONArray(List.of("integer", "uint64_hex", "boolean", "string", "enum")));
    }

    private JSONObject emit(JSONObject params) throws Exception {
        fields(params, "circuit", "path");
        Path path = absolutePath(params, "path");
        if (context != null) context.validateOutput(path);
        Path sidecarPath = path.resolveSibling(path.getFileName() + ".ids.json");
        JSONObject specification = object(params, "circuit");
        fields(specification, "schema_version", "instances", "wires", "label");
        integer(specification.opt("schema_version"), "schema_version", 1, 1);
        JSONArray instances = array(specification, "instances", MAX_INSTANCES);
        JSONArray segments = array(specification, "wires", MAX_WIRES);
        Circuit circuit = new Circuit();
        if (specification.has("label")) {
            Object raw = specification.get("label");
            if (!(raw instanceof String label) || label.length() > 512) throw invalid("RESOURCE_LIMIT", "Circuit label must be a string of at most 512 characters");
            circuit.getAttributes().set(de.neemann.digital.core.element.Keys.LABEL, label);
        }
        ArrayList<String> ids = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < instances.length(); i++) {
            JSONObject instance = arrayObject(instances, i);
            String id = string(instance, "id");
            if (context != null && !id.matches("[A-Za-z_][A-Za-z0-9_-]{0,127}")) {
                throw invalid("INVALID_COMPONENT_IDENTITY", "Package local IDs must be atoms without dots");
            }
            if (!seen.add(id)) throw invalid("DUPLICATE_INSTANCE", "Duplicate instance ID: " + id);
            ids.add(id);
            circuit.add(components.create(instance));
        }
        ArrayList<Wire> wires = new ArrayList<>();
        for (int i = 0; i < segments.length(); i++) {
            JSONObject segment = arrayObject(segments, i);
            fields(segment, "from", "to", "net_id");
            if (segment.has("net_id")) string(segment, "net_id");
            Vector from = Components.position(object(segment, "from"));
            Vector to = Components.position(object(segment, "to"));
            if (from.equals(to)) throw invalid("ZERO_LENGTH_WIRE", "Wire endpoints must differ");
            if (from.x != to.x && from.y != to.y) throw invalid("NON_ORTHOGONAL_WIRE", "Wires must be horizontal or vertical");
            wires.add(new Wire(from, to));
        }
        // Discard net IDs; recover electrical connections from the reloaded file.
        circuit.add(wires);
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        circuit.save(output);
        byte[] bytes = output.toByteArray();
        if (bytes.length > MAX_ARTIFACT_BYTES) throw invalid("RESOURCE_LIMIT", "Circuit artifact is too large");
        Circuit loaded = Circuit.loadCircuit(new ByteArrayInputStream(bytes), components.shapes);
        if (loaded.getElements().size() != ids.size()) throw new IllegalStateException("Serialization changed component count");
        JSONArray associations = new JSONArray();
        for (int i = 0; i < ids.size(); i++) {
            VisualElement before = circuit.getElements().get(i);
            VisualElement after = loaded.getElements().get(i);
            if (!SerializedIdentity.signature(before).equals(SerializedIdentity.signature(after))) {
                throw new IllegalStateException("Serialization changed component at index " + i);
            }
            associations.put(new JSONObject().put("id", ids.get(i)).put("index", i)
                    .put("signature", SerializedIdentity.signature(after)));
        }
        String hash = sha256(bytes);
        JSONObject sidecar = new JSONObject().put("schema_version", 1).put("sha256", hash).put("instances", associations);
        // Replace each file atomically at the candidate-specific path.
        writeAtomic(path, bytes);
        byte[] sidecarBytes = canonical(sidecar).getBytes(StandardCharsets.UTF_8);
        writeAtomic(sidecarPath, sidecarBytes);
        return new JSONObject().put("schema_version", 1).put("path", path.toString())
                .put("sidecar_path", sidecarPath.toString()).put("sha256", hash).put("sidecar_sha256", sha256(sidecarBytes));
    }

    private Loaded load(Path path, Path sidecarPath) throws Exception {
        if (context != null) {
            Circuit circuit = context.loadRoot(path, sidecarPath);
            return new Loaded(circuit, context.ids, context.rootCircuitHash, context.rootSidecarHash);
        }
        byte[] bytes = readBounded(path);
        Circuit circuit = Circuit.loadCircuit(new ByteArrayInputStream(bytes), components.shapes);
        circuit.setOrigin(path.toFile());
        if (circuit.getElements().size() > MAX_INSTANCES || circuit.getWires().size() > MAX_WIRES * 4) {
            throw invalid("RESOURCE_LIMIT", "Reloaded circuit exceeds component/wire limits");
        }
        for (VisualElement element : circuit.getElements()) components.validateLoaded(element);
        IdentityHashMap<VisualElement, String> ids = new IdentityHashMap<>();
        String sidecarHash = null;
        if (sidecarPath != null) {
            byte[] sidecarBytes = readBounded(sidecarPath);
            sidecarHash = sha256(sidecarBytes);
            JSONObject sidecar = new JSONObject(new String(sidecarBytes, StandardCharsets.UTF_8));
            fields(sidecar, "schema_version", "sha256", "instances");
            integer(sidecar.opt("schema_version"), "schema_version", 1, 1);
            if (!sha256(bytes).equals(string(sidecar, "sha256"))) {
                throw invalid("STALE_SIDECAR", "Circuit bytes differ from the instance sidecar; regenerate validation");
            }
            JSONArray associations = array(sidecar, "instances", MAX_INSTANCES);
            if (associations.length() != circuit.getElements().size()) {
                throw invalid("INSTANCE_ASSOCIATION", "Sidecar component count differs from the circuit");
            }
            Set<String> unique = new HashSet<>();
            for (int i = 0; i < associations.length(); i++) {
                JSONObject association = arrayObject(associations, i);
                fields(association, "id", "index", "signature");
                integer(association.opt("index"), "index", i, i);
                String id = string(association, "id");
                if (!unique.add(id)) throw invalid("INSTANCE_ASSOCIATION", "Sidecar contains duplicate IDs");
                VisualElement element = circuit.getElements().get(i);
                if (!SerializedIdentity.signature(element).equals(string(association, "signature"))) {
                    throw invalid("INSTANCE_ASSOCIATION", "Serialized component signature differs at index " + i);
                }
                ids.put(element, id);
            }
        }
        return new Loaded(circuit, ids, sha256(bytes), sidecarHash);
    }

    private JSONObject inspect(JSONObject params) throws Exception {
        fields(params, "path", "sidecar_path");
        Loaded loaded = load(absolutePath(params, "path"), absolutePath(params, "sidecar_path"));
        if (context != null) return inspectPackage(loaded);
        JSONObject report = new JSONObject().put("schema_version", 1).put("instances", describeInstances(loaded))
                .put("wires", wireDescriptions(loaded.circuit)).put("nets", new JSONArray())
                .put("unconnected", new JSONArray()).put("topology_available", false);
        JSONArray diagnostics = new JSONArray();
        Model model = null;
        try {
            NetList nets = new NetList(loaded.circuit);
            ModelCreator creator = new ModelCreator(loaded.circuit, components.library, false, nets, "", 0, null);
            IdentityHashMap<Pin, JSONObject> endpoints = endpoints(creator, loaded.ids);
            Set<Pin> connected = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
            ArrayList<JSONObject> groups = new ArrayList<>();
            for (Net net : nets) {
                ArrayList<JSONObject> members = new ArrayList<>();
                int drivers = 0;
                for (Pin pin : net.getPins()) {
                    if (!endpoints.containsKey(pin)) throw new IllegalStateException("Native net contains an untracked pin");
                    members.add(endpoints.get(pin));
                    connected.add(pin);
                    if (pin.getDirection() != Pin.Direction.input) drivers++;
                }
                members.sort(Comparator.comparing(JsonSupport::canonical));
                JSONObject group = new JSONObject().put("endpoints", new JSONArray(members));
                if (drivers > 1) diagnostics.put(diagnostic("MULTIPLE_DRIVERS", "An ordinary net has " + drivers + " drivers"));
                groups.add(group);
            }
            groups.sort(Comparator.comparing(JsonSupport::canonical));
            JSONArray actualNets = new JSONArray();
            for (int i = 0; i < groups.size(); i++) actualNets.put(groups.get(i).put("id", "actual_" + i));
            ArrayList<JSONObject> unconnected = new ArrayList<>();
            for (Map.Entry<Pin, JSONObject> item : endpoints.entrySet()) {
                if (!connected.contains(item.getKey())) unconnected.add(item.getValue());
            }
            unconnected.sort(Comparator.comparing(JsonSupport::canonical));
            report.put("nets", actualNets).put("unconnected", new JSONArray(unconnected)).put("topology_available", true);
            // Keep the topology above even when Digital rejects width mismatches or required open inputs.
            model = creator.createModel(true);
            attachObservedWidths(report.getJSONArray("instances"), creator, loaded.ids);
        } catch (Exception error) {
            diagnostics.put(diagnostic("DIGITAL_MODEL_INVALID", detail(error)));
        } finally {
            if (model != null) model.close();
        }
        return report.put("model", new JSONObject().put("valid", diagnostics.isEmpty()).put("diagnostics", diagnostics));
    }

    private JSONArray describeInstances(Loaded loaded) throws Exception {
        JSONArray result = new JSONArray();
        for (VisualElement element : loaded.circuit.getElements()) result.put(components.describe(element, loaded.ids.get(element)));
        return result;
    }

    private JSONObject inspectPackage(Loaded loaded) throws Exception {
        JSONObject report = new JSONObject().put("schema_version", 1).put("instances", describeInstances(loaded))
                .put("wires", wireDescriptions(loaded.circuit)).put("nets", new JSONArray())
                .put("unconnected", new JSONArray()).put("topology_available", false);
        JSONObject expanded = new JSONObject().put("schema_version", 1).put("instances", new JSONArray())
                .put("wires", new JSONArray()).put("nets", new JSONArray())
                .put("unconnected", new JSONArray()).put("topology_available", false);
        JSONArray diagnostics = new JSONArray();
        Model model = null;
        try {
            // Preserve canvas pins before ModelCreator merges custom boundaries in NetList.
            NetList physical = new NetList(loaded.circuit);
            IdentityHashMap<Pin, JSONObject> localEndpoints = new IdentityHashMap<>();
            for (VisualElement element : loaded.circuit.getElements()) {
                for (Pin pin : element.getPins()) {
                    physical.add(pin);
                    localEndpoints.put(pin, new JSONObject().put("instance_id", loaded.ids.get(element))
                            .put("direction", pin.getDirection().name()).put("pin", pin.getName()));
                }
            }
            describeNets(report, physical, localEndpoints, null);
            NetList nets = new NetList(loaded.circuit);
            ModelCreator creator = new ModelCreator(loaded.circuit, components.library, false, nets, "", 0, null);
            JSONArray instances = new JSONArray();
            for (ModelEntry entry : creator) {
                instances.put(components.describe(entry.getVisualElement(), loaded.ids.get(entry.getVisualElement())));
            }
            expanded.put("instances", instances);
            describeNets(expanded, nets, endpoints(creator, loaded.ids), diagnostics);
            model = creator.createModel(true);
            attachObservedWidths(instances, creator, loaded.ids);
            // Expose model-inferred widths on root canvas pins, including Tunnel pins.
            Map<String, JSONObject> observed = new LinkedHashMap<>();
            for (int i = 0; i < instances.length(); i++) observed.put(instances.getJSONObject(i).getString("id"), instances.getJSONObject(i));
            JSONArray local = report.getJSONArray("instances");
            for (int i = 0; i < local.length(); i++) {
                JSONObject item = local.getJSONObject(i), value = observed.get(item.getString("id"));
                if (value != null) item.put("pins", value.getJSONArray("pins"));
            }
        } catch (Exception error) {
            diagnostics.put(diagnostic("DIGITAL_MODEL_INVALID", detail(error)));
        } finally {
            if (model != null) model.close();
        }
        JSONObject validity = new JSONObject().put("valid", diagnostics.isEmpty()).put("diagnostics", diagnostics);
        return report.put("model", validity).put("expanded", expanded.put("model", validity));
    }

    private static void describeNets(JSONObject report, NetList nets, IdentityHashMap<Pin, JSONObject> endpoints,
                                     JSONArray diagnostics) {
        Set<Pin> connected = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
        ArrayList<JSONObject> groups = new ArrayList<>();
        for (Net net : nets) {
            ArrayList<JSONObject> members = new ArrayList<>();
            int drivers = 0;
            for (Pin pin : net.getPins()) {
                JSONObject endpoint = endpoints.get(pin);
                if (endpoint == null) throw new IllegalStateException("Native net contains an untracked pin");
                members.add(endpoint);
                connected.add(pin);
                if (pin.getDirection() != Pin.Direction.input) drivers++;
            }
            members.sort(Comparator.comparing(JsonSupport::canonical));
            groups.add(new JSONObject().put("endpoints", new JSONArray(members)));
            if (diagnostics != null && drivers > 1) diagnostics.put(diagnostic("MULTIPLE_DRIVERS", "Expanded net has " + drivers + " drivers"));
        }
        groups.sort(Comparator.comparing(JsonSupport::canonical));
        JSONArray actual = new JSONArray();
        for (int i = 0; i < groups.size(); i++) actual.put(groups.get(i).put("id", "actual_" + i));
        ArrayList<JSONObject> unconnected = new ArrayList<>();
        for (Map.Entry<Pin, JSONObject> item : endpoints.entrySet()) if (!connected.contains(item.getKey())) unconnected.add(item.getValue());
        unconnected.sort(Comparator.comparing(JsonSupport::canonical));
        report.put("nets", actual).put("unconnected", new JSONArray(unconnected)).put("topology_available", true);
    }

    private static IdentityHashMap<Pin, JSONObject> endpoints(ModelCreator creator, IdentityHashMap<VisualElement, String> ids) {
        IdentityHashMap<Pin, JSONObject> result = new IdentityHashMap<>();
        for (ModelEntry entry : creator) {
            String id = ids.get(entry.getVisualElement());
            if (id == null) throw new IllegalStateException("Native model contains an untracked instance");
            for (Pin pin : entry.getPins()) {
                result.put(pin, new JSONObject().put("instance_id", id).put("direction", pin.getDirection().name())
                        .put("pin", pin.getName()));
            }
        }
        return result;
    }

    private static void attachObservedWidths(JSONArray instances, ModelCreator creator,
                                             IdentityHashMap<VisualElement, String> ids) {
        Map<String, JSONObject> byId = new LinkedHashMap<>();
        for (int i = 0; i < instances.length(); i++) {
            JSONObject instance = instances.getJSONObject(i);
            byId.put(instance.getString("id"), instance);
        }
        for (ModelEntry entry : creator) {
            JSONArray descriptions = byId.get(ids.get(entry.getVisualElement())).getJSONArray("pins");
            for (Pin pin : entry.getPins()) {
                if (pin.getValue() == null) continue;
                for (int i = 0; i < descriptions.length(); i++) {
                    JSONObject description = descriptions.getJSONObject(i);
                    if (description.getString("direction").equals(pin.getDirection().name())
                            && description.getString("pin").equals(pin.getName())) {
                        description.put("width", pin.getValue().getBits());
                    }
                }
            }
        }
    }

    private JSONObject render(JSONObject params) throws Exception {
        fields(params, "path", "sidecar_path", "format", "output_path", "scale");
        Path input = absolutePath(params, "path");
        Path output = absolutePath(params, "output_path");
        Path sidecar = params.has("sidecar_path") ? absolutePath(params, "sidecar_path") : null;
        if (context != null) context.validatePreviewOutput(output);
        if (input.equals(output) || output.equals(sidecar)) throw invalid("OUTPUT_COLLISION", "Preview must have a different path from native circuit files");
        String format = string(params, "format");
        if (!format.equals("svg") && !format.equals("png")) throw invalid("INVALID_FORMAT", "Preview format must be svg or png");
        double scale = 1;
        if (params.has("scale")) {
            Object value = params.get("scale");
            if (!(value instanceof Number number)) throw invalid("INVALID_SCALE", "Scale must be numeric");
            scale = number.doubleValue();
        }
        if (!Double.isFinite(scale) || scale < 0.05 || scale > 8) throw invalid("INVALID_SCALE", "Scale must be between 0.05 and 8");
        Loaded loaded = load(input, sidecar);
        Circuit circuit = loaded.circuit;
        GraphicMinMax bounds = new GraphicMinMax();
        circuit.drawTo(bounds);
        if (!bounds.isValid()) throw invalid("EMPTY_PREVIEW", "Circuit has no drawable content");
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        JSONObject metadata = null;
        if (format.equals("png")) {
            long width = Math.round((bounds.getMax().getXFloat() - bounds.getMin().getXFloat() + Style.MAXLINETHICK * 2) * scale);
            long height = Math.round((bounds.getMax().getYFloat() - bounds.getMin().getYFloat() + Style.MAXLINETHICK * 2) * scale);
            if (width < 1 || height < 1 || width > MAX_PNG_DIMENSION || height > MAX_PNG_DIMENSION || width * height > MAX_PNG_PIXELS) {
                throw invalid("PREVIEW_RESOURCE_LIMIT", "Requested PNG dimensions exceed the bounded preview budget; reduce scale");
            }
            float renderScale = (float) scale;
            OpaquePng[] renderer = new OpaquePng[1];
            new Export(circuit, stream -> renderer[0] = new OpaquePng(stream, renderScale)).export(bytes);
            if (sidecar != null) {
                metadata = renderer[0].metadata();
                metadata.getJSONObject("image").put("sha256", sha256(bytes.toByteArray()));
                metadata.put("circuit", new JSONObject().put("sha256", loaded.circuitHash).put("sidecar_sha256", loaded.sidecarHash));
                JSONArray instances = new JSONArray();
                int pinCount = 0;
                for (VisualElement element : circuit.getElements()) {
                    JSONObject description = components.describe(element, loaded.ids.get(element));
                    JSONObject item = new JSONObject();
                    for (String key : List.of("id", "type", "position", "rotation", "bounds", "body_bounds")) item.put(key, description.get(key));
                    item.put("label", element.getElementAttributes().getLabel());
                    JSONArray pins = description.getJSONArray("pins");
                    pinCount += pins.length();
                    if (pinCount > 16384) throw invalid("PREVIEW_RESOURCE_LIMIT", "Preview metadata exceeds its pin budget");
                    for (int i = 0; i < pins.length(); i++) pins.getJSONObject(i).remove("escape");
                    item.put("pins", pins);
                    instances.put(item);
                }
                metadata.put("instances", instances);
                if (canonical(metadata).getBytes(StandardCharsets.UTF_8).length > 4 * 1024 * 1024) {
                    throw invalid("PREVIEW_RESOURCE_LIMIT", "Preview metadata exceeds 4 MiB");
                }
            }
        } else {
            new Export(circuit, stream -> new GraphicSVG(stream, null, 15)).export(bytes);
            String svg = bytes.toString(StandardCharsets.UTF_8).replaceAll("(?m)^<!-- created: .* -->\\r?\\n", "");
            bytes.reset();
            bytes.write(svg.getBytes(StandardCharsets.UTF_8));
        }
        writeAtomic(output, bytes.toByteArray());
        JSONObject result = new JSONObject().put("schema_version", 1).put("path", output.toString());
        if (metadata != null) result.put("metadata", metadata);
        return result;
    }

    private JSONObject simulateCycles(JSONObject params) throws Exception {
        fields(params, "path", "sidecar_path", "clock_instance_id", "initialization", "cycles");
        Loaded loaded = load(absolutePath(params, "path"), absolutePath(params, "sidecar_path"));
        return new CycleSimulator(components).simulate(loaded.circuit, loaded.ids, params);
    }

    private JSONObject simulate(JSONObject params) throws Exception {
        fields(params, "path", "sidecar_path", "stimuli");
        JSONArray stimuli = array(params, "stimuli", 100000);
        Loaded loaded = load(absolutePath(params, "path"), absolutePath(params, "sidecar_path"));
        Model model = null;
        try {
            NetList nets = new NetList(loaded.circuit);
            ModelCreator creator = new ModelCreator(loaded.circuit, components.library, false, nets, "", 0, null);
            for (Net net : nets) {
                int drivers = 0;
                for (Pin pin : net.getPins()) if (pin.getDirection() != Pin.Direction.input) drivers++;
                if (drivers > 1) throw invalid("MULTIPLE_DRIVERS", "Binary simulation requires at most one driver per net");
            }
            model = creator.createModel(true).setRecoverFromOscillation(false);
            ArrayList<Exception> errors = new ArrayList<>();
            model.addObserver(event -> {
                // Digital may notify an observer of more event types than it registered for.
                if (event.getType() == ModelEventType.ERROR_OCCURRED) {
                    errors.add(event.getCause() == null ? new IllegalStateException("Digital emitted an error without a cause") : event.getCause());
                }
            }, ModelEventType.ERROR_OCCURRED);
            Map<String, ObservableValue> inputs = new LinkedHashMap<>();
            Map<String, ObservableValue> outputs = new LinkedHashMap<>();
            for (ModelEntry entry : creator) {
                String id = loaded.ids.get(entry.getVisualElement());
                if (id == null) throw new IllegalStateException("Simulation contains an untracked instance");
                if (entry.getVisualElement().getElementName().equals("In")) {
                    inputs.put(id, entry.getElement().getOutputs().get(0));
                } else if (entry.getVisualElement().getElementName().equals("Out")) {
                    outputs.put(id, entry.getPins().getInputs().get("in").getValue());
                }
            }
            model.init(false);
            checkRunning(model, errors);
            JSONArray samples = new JSONArray();
            for (int i = 0; i < stimuli.length(); i++) {
                JSONObject stimulus = arrayObject(stimuli, i);
                fields(stimulus, "values");
                JSONObject values = object(stimulus, "values");
                if (!values.keySet().equals(inputs.keySet())) {
                    throw invalid("INPUT_SET_MISMATCH", "Each stimulus must provide exactly the input instance IDs: " + inputs.keySet());
                }
                for (Map.Entry<String, ObservableValue> entry : inputs.entrySet()) {
                    String value = string(values, entry.getKey());
                    if (!value.matches("0[xX][0-9a-fA-F]{1,16}")) throw invalid("INVALID_SIGNAL_VALUE", "Signal values must be uint64 hexadecimal strings");
                    BigInteger parsed = new BigInteger(value.substring(2), 16);
                    if (parsed.bitLength() > entry.getValue().getBits()) throw invalid("SIGNAL_WIDTH", "Value exceeds input width for " + entry.getKey());
                    entry.getValue().setValue(parsed.longValue());
                }
                model.doStep(false);
                checkRunning(model, errors);
                JSONObject valuesOut = new JSONObject();
                JSONObject highZ = new JSONObject();
                for (Map.Entry<String, ObservableValue> entry : outputs.entrySet()) {
                    valuesOut.put(entry.getKey(), "0x" + Long.toUnsignedString(entry.getValue().getValue(), 16));
                    highZ.put(entry.getKey(), "0x" + Long.toUnsignedString(entry.getValue().getHighZ(), 16));
                }
                samples.put(new JSONObject().put("values", valuesOut).put("high_z", highZ));
            }
            return new JSONObject().put("schema_version", 1).put("samples", samples);
        } finally {
            if (model != null) model.close();
        }
    }

    private static void checkRunning(Model model, List<Exception> errors) throws RpcException {
        if (!model.isRunning() || !errors.isEmpty()) {
            throw new RpcException(-32010, "SIMULATION_FAILED", errors.isEmpty() ? "Digital closed the model while settling" : detail(errors.get(0)));
        }
    }

    private static JSONArray wireDescriptions(Circuit circuit) {
        ArrayList<JSONObject> result = new ArrayList<>();
        for (Wire wire : circuit.getWires()) {
            Vector from = wire.p1;
            Vector to = wire.p2;
            if (from.x > to.x || from.x == to.x && from.y > to.y) {
                from = wire.p2;
                to = wire.p1;
            }
            result.add(new JSONObject().put("from", Components.point(from)).put("to", Components.point(to)));
        }
        result.sort(Comparator.comparing(JsonSupport::canonical));
        return new JSONArray(result);
    }

    private static byte[] readBounded(Path path) throws Exception {
        if (Files.size(path) > MAX_ARTIFACT_BYTES) throw invalid("RESOURCE_LIMIT", "Artifact exceeds 64 MiB");
        try (var input = Files.newInputStream(path)) {
            byte[] bytes = input.readNBytes((int) MAX_ARTIFACT_BYTES + 1);
            if (bytes.length > MAX_ARTIFACT_BYTES) throw invalid("RESOURCE_LIMIT", "Artifact exceeds 64 MiB");
            return bytes;
        }
    }

    private static void writeAtomic(Path path, byte[] bytes) throws Exception {
        if (path.getParent() == null || !Files.isDirectory(path.getParent())) {
            throw invalid("OUTPUT_DIRECTORY_MISSING", "Output parent directory must already exist");
        }
        Path temporary = Files.createTempFile(path.getParent(), ".v2d-", ".tmp");
        try {
            Files.write(temporary, bytes);
            Files.move(temporary, path, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } finally {
            Files.deleteIfExists(temporary);
        }
    }

    private static JSONObject diagnostic(String code, String message) {
        return new JSONObject().put("code", code).put("message", message);
    }

    private static String detail(Throwable error) {
        if (error == null) return "Digital operation failed without an exception cause";
        Throwable cause = error;
        Set<Throwable> visited = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
        while (cause.getCause() != null && visited.add(cause)) cause = cause.getCause();
        String message = cause.getMessage();
        return cause.getClass().getSimpleName() + ": " + (message == null ? "Digital operation failed" : message);
    }

    private record Loaded(Circuit circuit, IdentityHashMap<VisualElement, String> ids, String circuitHash, String sidecarHash) { }
}
