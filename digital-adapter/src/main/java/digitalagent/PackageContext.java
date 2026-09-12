package digitalagent;

import de.neemann.digital.core.NodeException;
import de.neemann.digital.core.element.ElementAttributes;
import de.neemann.digital.core.element.ElementTypeDescription;
import de.neemann.digital.core.element.Keys;
import de.neemann.digital.draw.elements.Circuit;
import de.neemann.digital.draw.elements.PinException;
import de.neemann.digital.draw.elements.VisualElement;
import de.neemann.digital.draw.library.CustomElement;
import de.neemann.digital.draw.library.ElementLibrary;
import de.neemann.digital.draw.library.ElementNotFoundException;
import de.neemann.digital.draw.library.ElementTypeDescriptionCustom;
import de.neemann.digital.draw.library.LibraryInterface;
import de.neemann.digital.draw.model.ModelCreator;
import de.neemann.digital.draw.model.NetList;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

import static digitalagent.JsonSupport.*;

/** One request's immutable, hash-bound local module inventory. Never scans a directory. */
final class PackageContext {
    static final int MAX_MODULES = 64;
    static final int MAX_DEPTH = 12;
    static final int MAX_OCCURRENCES = 1024;
    static final long MAX_BYTES = 64L * 1024 * 1024;
    final ScopedLibrary library = new ScopedLibrary();
    final Components components;
    final IdentityHashMap<VisualElement, String> ids = new IdentityHashMap<>();
    private final Map<String, Module> modules = new LinkedHashMap<>();
    private final Map<String, Integer> heights = new LinkedHashMap<>();
    private final Path root;
    private int occurrences;
    String rootCircuitHash, rootSidecarHash;

    PackageContext(JSONObject request) throws Exception {
        fields(request, "root", "modules");
        root = absolutePath(request, "root").toRealPath();
        if (!Files.isDirectory(root)) throw invalid("PACKAGE_ROOT", "Package root must be a directory");
        JSONArray rows = array(request, "modules", MAX_MODULES);
        components = new Components(this);
        long total = 0;
        for (int i = 0; i < rows.length(); i++) {
            JSONObject row = arrayObject(rows, i);
            fields(row, "type", "sha256", "sidecar_sha256");
            String type = string(row, "type");
            if (!type.matches("m_[0-9a-f]{24}\\.dig")) throw invalid("PACKAGE_TYPE", "Use generated m_<24hex>.dig basenames");
            if (modules.containsKey(type)) throw invalid("PACKAGE_DUPLICATE", "Duplicate module type: " + type);
            String hash = hashField(row, "sha256"), sidecarHash = hashField(row, "sidecar_sha256");
            Path path = owned(root.resolve(type));
            Path sidecar = owned(root.resolve(type + ".ids.json"));
            if (Files.size(path) + Files.size(sidecar) > MAX_BYTES - total) {
                throw invalid("PACKAGE_LIMIT", "Registered artifacts exceed 64 MiB total");
            }
            byte[] bytes = read(path), sidecarBytes = read(sidecar);
            total += bytes.length + sidecarBytes.length;
            if (total > MAX_BYTES) throw invalid("PACKAGE_LIMIT", "Registered artifacts exceed 64 MiB total");
            if (!sha256(bytes).equals(hash) || !sha256(sidecarBytes).equals(sidecarHash)) {
                throw invalid("PACKAGE_HASH", "Registered circuit or sidecar changed: " + type);
            }
            Circuit circuit = parse(bytes, path);
            Map<Integer, String> local = associations(circuit, bytes, sidecarBytes);
            modules.put(type, new Module(type, path, bytes, sidecarBytes, hash, sidecarHash, circuit, local));
        }
        for (String type : modules.keySet()) register(type, new HashSet<>(), 0);
    }

    boolean contains(String type) { return modules.containsKey(type); }

    private static String hashField(JSONObject object, String key) throws RpcException {
        String value = string(object, key);
        if (!value.matches("[0-9a-f]{64}")) throw invalid("PACKAGE_HASH", key + " must be a lowercase SHA-256 digest");
        return value;
    }

    private Path owned(Path path) throws Exception {
        Path real = path.toRealPath();
        if (!root.equals(real.getParent()) || !path.getFileName().equals(real.getFileName())) {
            throw invalid("PACKAGE_PATH", "Package files must be direct children of its root without redirected basenames");
        }
        return real;
    }

    void validatePreviewOutput(Path path) throws Exception {
        Path normalized = path.getParent().toRealPath().resolve(path.getFileName());
        for (Module module : modules.values()) {
            if (normalized.equals(module.path) || normalized.equals(module.path.resolveSibling(module.type + ".ids.json"))) {
                throw invalid("PACKAGE_IMMUTABLE", "Preview must not replace a registered circuit or sidecar");
            }
        }
    }

    void validateOutput(Path path) throws Exception {
        if (!root.equals(path.getParent().toRealPath()) || !path.getFileName().toString().matches("[A-Za-z_][A-Za-z0-9_-]*\\.dig")) {
            throw invalid("PACKAGE_PATH", "Circuit output must be a local ASCII .dig basename in the package root");
        }
        if (modules.containsKey(path.getFileName().toString())) {
            throw invalid("PACKAGE_IMMUTABLE", "Do not overwrite an already registered child module");
        }
    }

    void recheck() throws Exception {
        for (Module module : modules.values()) {
            if (!sha256(read(owned(module.path))).equals(module.hash)
                    || !sha256(read(owned(module.path.resolveSibling(module.type + ".ids.json")))).equals(module.sidecarHash)) {
                throw invalid("PACKAGE_HASH", "Registered artifact changed during the operation: " + module.type);
            }
        }
    }

    Circuit loadRoot(Path path, Path sidecarPath) throws Exception {
        path = owned(path);
        if (sidecarPath == null) sidecarPath = path.resolveSibling(path.getFileName() + ".ids.json");
        sidecarPath = owned(sidecarPath);
        if (!sidecarPath.equals(path.resolveSibling(path.getFileName() + ".ids.json"))) {
            throw invalid("PACKAGE_PATH", "A packaged circuit uses its adjacent .ids.json sidecar");
        }
        byte[] bytes = read(path), sidecarBytes = read(sidecarPath);
        rootCircuitHash = sha256(bytes);
        rootSidecarHash = sha256(sidecarBytes);
        Circuit circuit = parse(bytes, path);
        Map<Integer, String> local = associations(circuit, bytes, sidecarBytes);
        validate(circuit);
        assign(circuit, local, "");
        return circuit;
    }

    private Circuit parse(byte[] bytes, Path path) throws Exception {
        Circuit circuit = Circuit.loadCircuit(new ByteArrayInputStream(bytes), components.shapes);
        circuit.setOrigin(path.toFile());
        if (circuit.getAttributes().get(Keys.IS_GENERIC)) throw invalid("PACKAGE_GENERIC", "Generated modules must have resolved parameters");
        ElementAttributes allowed = new ElementAttributes();
        String label = circuit.getAttributes().getLabel();
        if (circuit.getAttributes().contains(Keys.LABEL)) allowed.set(Keys.LABEL, label);
        if (label.length() > 512 || !circuit.getAttributes().equals(allowed)) {
            throw invalid("PACKAGE_ATTRIBUTES", "Packaged circuit attributes may contain only a bounded display label");
        }
        if (circuit.getElements().size() > MAX_OCCURRENCES || circuit.getWires().size() > 80000) {
            throw invalid("PACKAGE_LIMIT", "Module exceeds the component or wire bound");
        }
        return circuit;
    }

    private static Map<Integer, String> associations(Circuit circuit, byte[] bytes, byte[] sidecarBytes) throws Exception {
        JSONObject sidecar = new JSONObject(new String(sidecarBytes, StandardCharsets.UTF_8));
        fields(sidecar, "schema_version", "sha256", "instances");
        integer(sidecar.opt("schema_version"), "schema_version", 1, 1);
        if (!sha256(bytes).equals(string(sidecar, "sha256"))) throw invalid("STALE_SIDECAR", "Packaged circuit differs from its sidecar");
        JSONArray rows = array(sidecar, "instances", MAX_OCCURRENCES);
        if (rows.length() != circuit.getElements().size()) throw invalid("INSTANCE_ASSOCIATION", "Sidecar component count differs");
        Map<Integer, String> result = new LinkedHashMap<>();
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < rows.length(); i++) {
            JSONObject row = arrayObject(rows, i);
            fields(row, "index", "id", "signature");
            integer(row.opt("index"), "index", i, i);
            String id = string(row, "id");
            if (!id.matches("[A-Za-z_][A-Za-z0-9_-]{0,127}") || !seen.add(id)) {
                throw invalid("INSTANCE_ASSOCIATION", "Package local IDs must be unique atoms without dots");
            }
            if (!SerializedIdentity.signature(circuit.getElements().get(i)).equals(string(row, "signature"))) {
                throw invalid("INSTANCE_ASSOCIATION", "Packaged element signature differs at index " + i);
            }
            result.put(i, id);
        }
        return result;
    }

    private void register(String type, Set<String> active, int depth) throws Exception {
        if (depth > MAX_DEPTH) throw invalid("PACKAGE_DEPTH", "Package hierarchy exceeds depth 12");
        if (active.contains(type)) throw invalid("PACKAGE_CYCLE", "Recursive custom module: " + type);
        if (library.custom.containsKey(type)) {
            if (depth + heights.get(type) > MAX_DEPTH) throw invalid("PACKAGE_DEPTH", "Package hierarchy exceeds depth 12");
            return;
        }
        Module module = modules.get(type);
        if (module == null) throw invalid("PACKAGE_MISSING", "Unregistered child module: " + type);
        active.add(type);
        int height = 0;
        for (VisualElement element : module.circuit.getElements()) {
            String child = element.getElementName();
            if (!Components.SUPPORTED.contains(child)) {
                register(child, active, depth + 1);
                height = Math.max(height, 1 + heights.get(child));
            }
        }
        active.remove(type);
        validate(module.circuit);
        ElementTypeDescriptionCustom description = ElementLibrary.createCustomDescription(module.path.toFile(), module.circuit, library);
        String label = module.circuit.getAttributes().getLabel();
        if (!label.isEmpty()) description.setShortName(label);
        description.setElementFactory(attributes -> new CustomElement(description) {
            @Override
            public ModelCreator getModelCreator(LibraryInterface lib, String subName, int nesting,
                                                VisualElement errorElement, VisualElement occurrence)
                    throws PinException, NodeException, ElementNotFoundException {
                String scope = ids.get(occurrence);
                try {
                    if (scope == null || nesting > MAX_DEPTH) throw invalid("PACKAGE_DEPTH", "Invalid or overly deep child occurrence");
                    // Create fresh elements per occurrence; ModelCreator merges custom ports.
                    Circuit child = parse(module.bytes, module.path);
                    assign(child, module.local, scope);
                    return new ModelCreator(child, lib, true, new NetList(child), subName, nesting, errorElement);
                } catch (PinException | NodeException | ElementNotFoundException error) {
                    throw error;
                } catch (Exception error) {
                    throw new NodeException("Cannot construct child occurrence " + scope, error);
                }
            }
        });
        library.custom.put(type, description);
        heights.put(type, height);
    }

    private void validate(Circuit circuit) throws Exception {
        for (VisualElement element : circuit.getElements()) components.validateLoaded(element);
        // Use native pin discovery for label validation and GUI-consistent port metadata.
        circuit.getInputNames();
        circuit.getOutputNames();
    }

    private void assign(Circuit circuit, Map<Integer, String> local, String scope) throws RpcException {
        for (int i = 0; i < circuit.getElements().size(); i++) {
            String id = (scope.isEmpty() ? "" : scope + ".") + local.get(i);
            if (id.length() > 128 || ++occurrences > MAX_OCCURRENCES) {
                throw invalid("PACKAGE_LIMIT", "Expanded occurrence count or scoped ID length exceeds its bound");
            }
            ids.put(circuit.getElements().get(i), id);
        }
    }

    private static byte[] read(Path path) throws Exception {
        if (Files.size(path) > MAX_BYTES) throw invalid("PACKAGE_LIMIT", "Package artifact exceeds 64 MiB");
        try (var input = Files.newInputStream(path)) {
            byte[] bytes = input.readNBytes((int) MAX_BYTES + 1);
            if (bytes.length > MAX_BYTES) throw invalid("PACKAGE_LIMIT", "Package artifact exceeds 64 MiB");
            return bytes;
        }
    }

    static final class ScopedLibrary extends ElementLibrary {
        private final Map<String, ElementTypeDescriptionCustom> custom = new LinkedHashMap<>();
        @Override public ElementTypeDescription getElementType(String name) throws ElementNotFoundException {
            if (custom.containsKey(name)) return custom.get(name);
            if (Components.SUPPORTED.contains(name)) return super.getElementType(name);
            throw new ElementNotFoundException("Component is outside the registered package: " + name);
        }
        @Override public ElementTypeDescription getElementType(String name, ElementAttributes attributes) throws ElementNotFoundException {
            return getElementType(name);
        }
    }

    private record Module(String type, Path path, byte[] bytes, byte[] sidecarBytes, String hash,
                          String sidecarHash, Circuit circuit, Map<Integer, String> local) { }
}
