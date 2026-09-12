package digitalagent;

import de.neemann.digital.draw.elements.VisualElement;
import org.eclipse.elk.alg.layered.options.LayerConstraint;
import org.eclipse.elk.alg.layered.options.LayeredOptions;
import org.eclipse.elk.alg.layered.options.OrderingStrategy;
import org.eclipse.elk.core.RecursiveGraphLayoutEngine;
import org.eclipse.elk.core.math.ElkPadding;
import org.eclipse.elk.core.math.KVector;
import org.eclipse.elk.core.options.CoreOptions;
import org.eclipse.elk.core.options.Direction;
import org.eclipse.elk.core.options.EdgeRouting;
import org.eclipse.elk.core.options.PortConstraints;
import org.eclipse.elk.core.options.PortSide;
import org.eclipse.elk.core.options.SizeConstraint;
import org.eclipse.elk.core.util.BasicProgressMonitor;
import org.eclipse.elk.graph.ElkBendPoint;
import org.eclipse.elk.graph.ElkEdge;
import org.eclipse.elk.graph.ElkEdgeSection;
import org.eclipse.elk.graph.ElkNode;
import org.eclipse.elk.graph.ElkPort;
import org.eclipse.elk.graph.util.ElkGraphUtil;
import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.EnumSet;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.function.LongSupplier;

import static digitalagent.JsonSupport.*;

/** Bounded geometry proposals. Digital wiring legality is checked by the caller. */
final class LayoutPlanner {
    static final int MAX_INSTANCES = 250;
    static final int MAX_ENDPOINTS = 16384;
    static final int MAX_WIRES = 20000;
    static final long TIMEOUT_NANOS = 15_000_000_000L;
    private final Components components;
    private final LongSupplier clock;
    private final long timeout;

    LayoutPlanner(Components components) {
        this(components, System::nanoTime, TIMEOUT_NANOS);
    }

    LayoutPlanner(Components components, LongSupplier clock, long timeout) {
        this.components = components;
        this.clock = clock;
        this.timeout = timeout;
    }

    JSONObject plan(JSONObject params) throws Exception {
        fields(params, "instances", "nets", "spacing", "seed");
        int spacing = (int) integer(params.has("spacing") ? params.get("spacing") : 40,
                "spacing", 40, 400);
        if (spacing % 20 != 0) throw invalid("INVALID_SPACING", "spacing must be a multiple of 20");
        integer(params.has("seed") ? params.get("seed") : 1, "seed", 1, 1);
        DeadlineMonitor monitor = new DeadlineMonitor(clock.getAsLong() + timeout, clock);
        requireTime(monitor);
        JSONArray requested = array(params, "instances", MAX_INSTANCES);
        if (requested.isEmpty()) throw invalid("EMPTY_LAYOUT", "At least one component is required");
        JSONArray requestedNets = array(params, "nets", MAX_ENDPOINTS);
        TreeMap<String, JSONObject> specifications = new TreeMap<>();
        for (int i = 0; i < requested.length(); i++) {
            JSONObject specification = arrayObject(requested, i);
            String id = identifier(specification, "id");
            if (specifications.put(id, specification) != null) {
                throw invalid("DUPLICATE_INSTANCE", "Duplicate component: " + id);
            }
        }

        ElkNode graph = ElkGraphUtil.createGraph();
        configure(graph, spacing);
        TreeMap<String, NodeData> nodes = new TreeMap<>();
        TreeMap<PinKey, PortData> ports = new TreeMap<>();
        for (Map.Entry<String, JSONObject> item : specifications.entrySet()) {
            requireTime(monitor);
            VisualElement element = components.create(item.getValue());
            if (element.getPos().x != 0 || element.getPos().y != 0) {
                throw invalid("LAYOUT_ORIGIN_REQUIRED", "Layout input components must be at the origin");
            }
            JSONObject description = components.describe(element, item.getKey());
            JSONObject bounds = description.getJSONObject("bounds");
            Point min = Point.read(bounds.getJSONObject("min"));
            Point max = Point.read(bounds.getJSONObject("max"));
            int left = floor(min.x - spacing, 20), top = floor(min.y - spacing, 20);
            int right = ceil(max.x + spacing, 20), bottom = ceil(max.y + spacing, 20);
            ElkNode node = ElkGraphUtil.createNode(graph);
            node.setIdentifier(item.getKey());
            node.setDimensions(right - left, bottom - top);
            node.setProperty(CoreOptions.PORT_CONSTRAINTS, PortConstraints.FIXED_POS);
            node.setProperty(CoreOptions.NODE_SIZE_CONSTRAINTS, EnumSet.noneOf(SizeConstraint.class));
            if (element.getElementName().equals("In")) {
                node.setProperty(LayeredOptions.LAYERING_LAYER_CONSTRAINT, LayerConstraint.FIRST);
            } else if (element.getElementName().equals("Out")) {
                node.setProperty(LayeredOptions.LAYERING_LAYER_CONSTRAINT, LayerConstraint.LAST);
            }
            NodeData data = new NodeData(item.getKey(), node, element.getRotate() * 90,
                    new Point(left, top), new Point(right, bottom));
            nodes.put(data.id, data);
            JSONArray nativePins = description.getJSONArray("pins");
            if (ports.size() + nativePins.length() > MAX_ENDPOINTS) {
                throw invalid("RESOURCE_LIMIT", "Layout exceeds " + MAX_ENDPOINTS + " native pins");
            }
            HashSet<Point> exits = new HashSet<>();
            ArrayList<JSONObject> orderedPins = new ArrayList<>();
            for (int i = 0; i < nativePins.length(); i++) orderedPins.add(nativePins.getJSONObject(i));
            orderedPins.sort(Comparator.comparing((JSONObject pin) -> pin.getString("direction"))
                    .thenComparing(pin -> pin.getString("pin")));
            for (JSONObject pin : orderedPins) {
                PinKey key = new PinKey(data.id, pin.getString("direction"), pin.getString("pin"));
                int width = (int) integer(pin.opt("width"), "native pin width", 1, 64);
                Point position = Point.read(pin.getJSONObject("position"));
                Point escape = Point.read(pin.getJSONObject("escape"));
                PortSide side = side(escape);
                Point exit = new Point(escape.x < 0 ? left : escape.x > 0 ? right : position.x,
                        escape.y < 0 ? top : escape.y > 0 ? bottom : position.y);
                if (!exits.add(exit)) throw invalid("COINCIDENT_PORT_EXITS", "Native pin escape rays coincide on " + data.id);
                ElkPort port = ElkGraphUtil.createPort(node);
                port.setIdentifier(key.direction + ":" + key.pin);
                port.setDimensions(0, 0);
                port.setLocation(exit.x - left, exit.y - top);
                port.setProperty(CoreOptions.PORT_SIDE, side);
                port.setProperty(CoreOptions.PORT_ANCHOR, new KVector(0, 0));
                ports.put(key, new PortData(key, data, port, width, position, escape, exit));
            }
        }

        TreeMap<String, List<PortData>> nets = readNets(requestedNets, ports);
        ArrayList<EdgeData> edges = new ArrayList<>();
        for (Map.Entry<String, List<PortData>> net : nets.entrySet()) {
            PortData source = net.getValue().stream().filter(pin -> pin.key.direction.equals("output"))
                    .findFirst().orElseThrow();
            for (PortData sink : net.getValue()) {
                if (sink == source) continue;
                // ELK 0.12 rejects hyperedges; share the source port to represent fanout.
                ElkEdge edge = ElkGraphUtil.createSimpleEdge(source.port, sink.port);
                edge.setIdentifier(net.getKey() + ":" + edges.size());
                edges.add(new EdgeData(net.getKey(), edge));
            }
        }
        requireTime(monitor);
        try {
            new RecursiveGraphLayoutEngine().layout(graph, monitor);
        } catch (RuntimeException error) {
            requireTime(monitor);
            throw new RpcException(-32000, "LAYOUT_FAILED", "ELK could not plan this graph: " + error.getMessage());
        }
        requireTime(monitor);
        return result(nodes, ports, edges, monitor);
    }

    private static void configure(ElkNode graph, int spacing) {
        graph.setProperty(CoreOptions.ALGORITHM, "org.eclipse.elk.layered");
        graph.setProperty(CoreOptions.DIRECTION, Direction.RIGHT);
        graph.setProperty(CoreOptions.EDGE_ROUTING, EdgeRouting.ORTHOGONAL);
        graph.setProperty(CoreOptions.RANDOM_SEED, 1);
        graph.setProperty(CoreOptions.PADDING, new ElkPadding(40));
        graph.setProperty(CoreOptions.SPACING_NODE_NODE, (double) spacing);
        graph.setProperty(CoreOptions.SPACING_EDGE_EDGE, 20.0);
        graph.setProperty(CoreOptions.SPACING_EDGE_NODE, 20.0);
        graph.setProperty(CoreOptions.SPACING_COMPONENT_COMPONENT, (double) spacing);
        graph.setProperty(CoreOptions.SEPARATE_CONNECTED_COMPONENTS, true);
        graph.setProperty(LayeredOptions.SPACING_NODE_NODE_BETWEEN_LAYERS, 2.0 * spacing);
        graph.setProperty(LayeredOptions.SPACING_EDGE_EDGE_BETWEEN_LAYERS, 20.0);
        graph.setProperty(LayeredOptions.SPACING_EDGE_NODE_BETWEEN_LAYERS, 20.0);
        graph.setProperty(LayeredOptions.ALLOW_NON_FLOW_PORTS_TO_SWITCH_SIDES, false);
        graph.setProperty(LayeredOptions.MERGE_EDGES, false);
        graph.setProperty(LayeredOptions.CONSIDER_MODEL_ORDER_STRATEGY, OrderingStrategy.NODES_AND_EDGES);
        graph.setProperty(LayeredOptions.THOROUGHNESS, 7);
    }

    private static TreeMap<String, List<PortData>> readNets(JSONArray requested, TreeMap<PinKey, PortData> ports)
            throws RpcException {
        TreeMap<String, List<PortData>> result = new TreeMap<>();
        int count = 0;
        for (int i = 0; i < requested.length(); i++) {
            JSONObject net = arrayObject(requested, i);
            fields(net, "id", "endpoints");
            String id = identifier(net, "id");
            if (result.containsKey(id)) throw invalid("DUPLICATE_NET", "Duplicate net: " + id);
            JSONArray endpoints = array(net, "endpoints", MAX_ENDPOINTS);
            count += endpoints.length();
            if (count > MAX_ENDPOINTS) throw invalid("RESOURCE_LIMIT", "Too many net endpoints");
            ArrayList<PortData> members = new ArrayList<>();
            int drivers = 0, width = -1;
            for (int j = 0; j < endpoints.length(); j++) {
                JSONObject endpoint = arrayObject(endpoints, j);
                fields(endpoint, "instance_id", "direction", "pin");
                PinKey key = new PinKey(identifier(endpoint, "instance_id"), string(endpoint, "direction"), string(endpoint, "pin"));
                PortData port = ports.get(key);
                if (port == null) throw invalid("UNKNOWN_ENDPOINT", "Unknown native endpoint: " + key);
                if (port.net != null) throw invalid("DUPLICATE_ENDPOINT", "Endpoint appears more than once: " + key);
                port.net = id;
                if (key.direction.equals("output")) drivers++;
                if (width != -1 && width != port.width) throw invalid("NET_WIDTH_MISMATCH", "Native widths differ on " + id);
                width = port.width;
                members.add(port);
            }
            if (drivers != 1) throw invalid("INVALID_NET_DRIVER", "Net " + id + " requires exactly one output driver");
            members.sort(Comparator.comparing(port -> port.key));
            result.put(id, members);
        }
        for (PortData port : ports.values()) {
            if (port.net == null) throw invalid("MISSING_ENDPOINT", "No net assigned to " + port.key);
        }
        return result;
    }

    private static JSONObject result(TreeMap<String, NodeData> nodes, TreeMap<PinKey, PortData> ports,
                                     List<EdgeData> edges, DeadlineMonitor monitor) throws RpcException {
        JSONArray placements = new JSONArray(), envelopes = new JSONArray(), pins = new JSONArray();
        ArrayList<WireData> wires = new ArrayList<>();
        for (NodeData node : nodes.values()) {
            requireTime(monitor);
            node.anchor = new Point(snap(node.node.getX() - node.min.x, 20), snap(node.node.getY() - node.min.y, 20));
            if (Math.abs(node.node.getWidth() - (node.max.x - node.min.x)) > 0.001
                    || Math.abs(node.node.getHeight() - (node.max.y - node.min.y)) > 0.001) {
                throw new RpcException(-32000, "FIXED_GEOMETRY_CHANGED", "ELK resized native envelope " + node.id);
            }
            placements.put(new JSONObject().put("id", node.id).put("position", node.anchor.json()).put("rotation", node.rotation));
            envelopes.put(new JSONObject().put("id", node.id).put("bounds", new JSONObject()
                    .put("min", node.min.add(node.anchor).json()).put("max", node.max.add(node.anchor).json())));
        }
        for (PortData port : ports.values()) {
            double expectedX = port.exit.x - port.node.min.x, expectedY = port.exit.y - port.node.min.y;
            if (Math.abs(port.port.getX() - expectedX) > 0.001 || Math.abs(port.port.getY() - expectedY) > 0.001) {
                throw new RpcException(-32000, "FIXED_GEOMETRY_CHANGED", "ELK moved fixed native port " + port.key);
            }
            Point position = port.position.add(port.node.anchor), exit = port.exit.add(port.node.anchor);
            pins.put(new JSONObject().put("instance_id", port.key.instance).put("direction", port.key.direction)
                    .put("pin", port.key.pin).put("net_id", port.net).put("width", port.width)
                    .put("position", position.json()).put("escape", port.escape.json()).put("exit", exit.json()));
            // Singleton output pins have no electrical wire to route.
            if (!port.port.getIncomingEdges().isEmpty() || !port.port.getOutgoingEdges().isEmpty()) {
                addWire(wires, position, exit, port.net);
            }
        }
        for (EdgeData edge : edges) {
            requireTime(monitor);
            if (edge.edge.getSections().isEmpty()) {
                throw new RpcException(-32000, "MISSING_ROUTE", "ELK returned no route for " + edge.net);
            }
            for (ElkEdgeSection section : edge.edge.getSections()) {
                Point previous = new Point(snap(section.getStartX(), 10), snap(section.getStartY(), 10));
                for (ElkBendPoint bend : section.getBendPoints()) {
                    Point next = new Point(snap(bend.getX(), 10), snap(bend.getY(), 10));
                    addWire(wires, previous, next, edge.net);
                    previous = next;
                }
                addWire(wires, previous, new Point(snap(section.getEndX(), 10), snap(section.getEndY(), 10)), edge.net);
            }
        }
        // Sort and deduplicate hints to make proposals independent of caller order.
        wires.sort(Comparator.comparing(WireData::net).thenComparing(WireData::from).thenComparing(WireData::to));
        JSONArray segments = new JSONArray();
        WireData previous = null;
        for (WireData wire : wires) {
            if (!wire.equals(previous)) segments.put(new JSONObject().put("from", wire.from.json())
                    .put("to", wire.to.json()).put("net_id", wire.net));
            previous = wire;
        }
        return new JSONObject().put("schema_version", 1).put("strategy", "elk-layered-v1")
                .put("elk_version", "0.12.0").put("seed", 1).put("placement_grid", 20).put("wire_grid", 10)
                .put("proposal_only", true).put("placements", placements).put("wires", segments)
                .put("ports", pins).put("envelopes", envelopes).put("diagnostics", new JSONArray());
    }

    private static void addWire(List<WireData> wires, Point from, Point to, String net) throws RpcException {
        if (from.equals(to)) return;
        if (from.x != to.x && from.y != to.y) {
            throw new RpcException(-32000, "NON_ORTHOGONAL_LAYOUT", "ELK proposed a diagonal segment for " + net);
        }
        if (wires.size() >= MAX_WIRES) throw invalid("RESOURCE_LIMIT", "Layout exceeds " + MAX_WIRES + " hint segments");
        wires.add(from.compareTo(to) < 0 ? new WireData(net, from, to) : new WireData(net, to, from));
    }

    private static String identifier(JSONObject object, String key) throws RpcException {
        String id = string(object, key);
        if (id.length() > 128 || !id.matches("[A-Za-z_][A-Za-z0-9_.-]*")) {
            throw invalid("INVALID_ID", key + " must be an ASCII identifier of at most 128 characters");
        }
        return id;
    }

    private static PortSide side(Point escape) throws RpcException {
        if (escape.equals(new Point(-1, 0))) return PortSide.WEST;
        if (escape.equals(new Point(1, 0))) return PortSide.EAST;
        if (escape.equals(new Point(0, -1))) return PortSide.NORTH;
        if (escape.equals(new Point(0, 1))) return PortSide.SOUTH;
        throw invalid("INVALID_PIN_ESCAPE", "Native pin has no cardinal escape vector");
    }

    private static int floor(int value, int grid) { return Math.floorDiv(value, grid) * grid; }
    private static int ceil(int value, int grid) { return -Math.floorDiv(-value, grid) * grid; }

    private static int snap(double value, int grid) throws RpcException {
        if (!Double.isFinite(value) || Math.abs(value) > Components.COORDINATE_LIMIT - 1000) {
            throw invalid("LAYOUT_COORDINATE_LIMIT", "Proposed layout exceeds the coordinate limit");
        }
        return (int) Math.round(value / grid) * grid;
    }

    private static void requireTime(DeadlineMonitor monitor) throws RpcException {
        if (monitor.isCanceled()) throw new RpcException(-32000, "LAYOUT_TIMEOUT", "Layout planning exceeded its deadline");
    }

    static final class DeadlineMonitor extends BasicProgressMonitor {
        private final long deadline;
        private final LongSupplier clock;

        DeadlineMonitor(long deadline, LongSupplier clock) {
            this.deadline = deadline;
            this.clock = clock;
        }

        @Override public boolean isCanceled() {
            return Thread.currentThread().isInterrupted() || clock.getAsLong() - deadline >= 0;
        }

        @Override protected BasicProgressMonitor doSubTask(float work, int maxLevels) {
            return new DeadlineMonitor(deadline, clock).withMaxHierarchyLevels(maxLevels);
        }
    }

    private record Point(int x, int y) implements Comparable<Point> {
        static Point read(JSONObject point) { return new Point(point.getInt("x"), point.getInt("y")); }
        JSONObject json() throws RpcException {
            if (Math.abs((long) x) > Components.COORDINATE_LIMIT || Math.abs((long) y) > Components.COORDINATE_LIMIT) {
                throw invalid("LAYOUT_COORDINATE_LIMIT", "Proposed geometry exceeds the coordinate limit");
            }
            return new JSONObject().put("x", x).put("y", y);
        }
        Point add(Point other) { return new Point(x + other.x, y + other.y); }
        @Override public int compareTo(Point other) {
            int order = Integer.compare(x, other.x);
            return order == 0 ? Integer.compare(y, other.y) : order;
        }
    }

    private record PinKey(String instance, String direction, String pin) implements Comparable<PinKey> {
        @Override public int compareTo(PinKey other) {
            int order = instance.compareTo(other.instance);
            if (order == 0) order = direction.compareTo(other.direction);
            return order == 0 ? pin.compareTo(other.pin) : order;
        }
    }

    private static final class NodeData {
        final String id;
        final ElkNode node;
        final int rotation;
        final Point min, max;
        Point anchor;
        NodeData(String id, ElkNode node, int rotation, Point min, Point max) {
            this.id = id;
            this.node = node;
            this.rotation = rotation;
            this.min = min;
            this.max = max;
        }
    }

    private static final class PortData {
        final PinKey key;
        final NodeData node;
        final ElkPort port;
        final int width;
        final Point position, escape, exit;
        String net;
        PortData(PinKey key, NodeData node, ElkPort port, int width, Point position, Point escape, Point exit) {
            this.key = key;
            this.node = node;
            this.port = port;
            this.width = width;
            this.position = position;
            this.escape = escape;
            this.exit = exit;
        }
    }

    private record EdgeData(String net, ElkEdge edge) { }
    private record WireData(String net, Point from, Point to) { }
}
