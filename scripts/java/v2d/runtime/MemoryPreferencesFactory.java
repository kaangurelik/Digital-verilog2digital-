package v2d.runtime;

import java.util.HashMap;
import java.util.Map;
import java.util.prefs.AbstractPreferences;
import java.util.prefs.Preferences;
import java.util.prefs.PreferencesFactory;

/** Process-local preferences for reproducible headless workers and tests. */
public final class MemoryPreferencesFactory implements PreferencesFactory {
    private final Node user = new Node(null, "");
    private final Node system = new Node(null, "");

    public Preferences userRoot() { return user; }
    public Preferences systemRoot() { return system; }

    private static final class Node extends AbstractPreferences {
        private final Map<String, String> values = new HashMap<>();
        private final Map<String, Node> nodes = new HashMap<>();

        Node(Node parent, String name) { super(parent, name); }
        protected void putSpi(String key, String value) { values.put(key, value); }
        protected String getSpi(String key) { return values.get(key); }
        protected void removeSpi(String key) { values.remove(key); }
        protected String[] keysSpi() { return values.keySet().toArray(new String[0]); }
        protected String[] childrenNamesSpi() { return nodes.keySet().toArray(new String[0]); }
        protected AbstractPreferences childSpi(String name) {
            return nodes.computeIfAbsent(name, n -> new Node(this, n));
        }
        protected void removeNodeSpi() {
            values.clear();
            nodes.clear();
            if (parent() != null) ((Node) parent()).nodes.remove(name());
        }
        protected void syncSpi() { }
        protected void flushSpi() { }
    }
}
