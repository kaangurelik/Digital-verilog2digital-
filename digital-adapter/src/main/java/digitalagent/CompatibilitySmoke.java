package digitalagent;

import de.neemann.digital.core.Model;
import de.neemann.digital.core.element.Keys;
import de.neemann.digital.draw.elements.Circuit;
import de.neemann.digital.draw.elements.VisualElement;
import de.neemann.digital.draw.elements.Wire;
import de.neemann.digital.draw.graphics.Vector;
import de.neemann.digital.draw.library.ElementLibrary;
import de.neemann.digital.draw.model.ModelCreator;
import de.neemann.digital.draw.shapes.ShapeFactory;
import org.eclipse.elk.core.RecursiveGraphLayoutEngine;
import org.eclipse.elk.core.options.CoreOptions;
import org.eclipse.elk.core.util.BasicProgressMonitor;
import org.eclipse.elk.graph.ElkNode;
import org.eclipse.elk.graph.util.ElkGraphUtil;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;

/** Exercises Digital serialization/simulation and ELK in the same JVM. */
public final class CompatibilitySmoke {
    public static void main(String[] args) throws Exception {
        ElementLibrary library = new ElementLibrary();
        ShapeFactory shapes = new ShapeFactory(library, true);
        Circuit circuit = new Circuit();
        circuit.add(new VisualElement("In").setShapeFactory(shapes).setPos(new Vector(0, 0)).setAttribute(Keys.LABEL, "a"));
        circuit.add(new VisualElement("Out").setShapeFactory(shapes).setPos(new Vector(160, 0)).setAttribute(Keys.LABEL, "y"));
        circuit.add(new Wire(new Vector(0, 0), new Vector(160, 0)));
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        circuit.save(bytes);
        Circuit loaded = Circuit.loadCircuit(new ByteArrayInputStream(bytes.toByteArray()), shapes);
        Model model = new ModelCreator(loaded, library).createModel(true);
        model.init(false);
        model.getInputs().get(0).getValue().setValue(1);
        model.doStep();
        if (model.getOutputs().get(0).getValue().getValue() != 1) {
            throw new IllegalStateException("Digital round-trip simulation failed");
        }

        ElkNode graph = ElkGraphUtil.createGraph();
        graph.setProperty(CoreOptions.ALGORITHM, "org.eclipse.elk.layered");
        ElkNode source = ElkGraphUtil.createNode(graph);
        source.setDimensions(40, 40);
        ElkNode target = ElkGraphUtil.createNode(graph);
        target.setDimensions(40, 40);
        ElkGraphUtil.createSimpleEdge(source, target);
        new RecursiveGraphLayoutEngine().layout(graph, new BasicProgressMonitor());
        if (source.getX() == target.getX() && source.getY() == target.getY()) {
            throw new IllegalStateException("ELK did not place the nodes");
        }
        System.out.println(new JSONObject().put("digital_round_trip", true).put("digital_simulation", true)
                .put("elk_layout", true).put("java", System.getProperty("java.version")));
    }
}
