# Desktop application

The desktop uses the same conversion and validation pipeline as `v2d`.

From the repository, build and launch with the isolated Python environment:

```powershell
.\.venv\Scripts\python.exe scripts/build.py gui
.\.venv\Scripts\python.exe scripts/run_gui.py
```

Add Verilog files in compilation order, or open a saved project. **Inspect tops** discovers the available top modules and reports unsupported source constructs. Select the top, choose a generated output folder, and use **Convert**. The converter keeps an immutable verified revision and only selects a replacement after its checks pass.

The Project tab supports include folders, defines, parameter overrides, and an optional sequential trace JSON. Saved paths are relative to the project file; Save As rebases them to the new location. The source order is preserved. Layout settings control spacing, hierarchy, native component profiles and optional tunnels. The default keeps hierarchy and uses visible wires. Settings a project omits take the same defaults as `v2d`, so a project converts alike in both.

The preview is rendered by Digital. Scroll to zoom, drag to pan, or use the Fit button. When the preview has keyboard focus, `+`, `-`, `0` and the arrow keys control its view. Click a component to see its stable identity. New results include checked geometry overlays; older circuit packages remain viewable without overlays. When refinement produced a comparison image, the view selector can show the layout before refinement.

Fitting a wide circuit into a small window can crowd the optional component-ID labels. Zoom in or disable **Component overlays** to read the underlying native circuit; hiding the overlay does not change the exported circuit.

Local model refinement starts disabled. The measured small-model default is Qwen3 4B Q4. The Resources tab exposes context, model idle time, and memory ceilings. These ceilings limit commitment; they do not allocate or reserve that much RAM. Model admission uses current available RAM and VRAM. Selecting the 14B preset chooses the measured 2048-token context, 11 GiB model ceiling and 12 GiB whole-job ceiling; it remains subject to fresh admission checks. Model Status checks file presence, Verify checks the pinned hashes, and Install explicitly downloads the selected pinned artifacts.

Cancel requests graceful job cancellation and bounds cleanup if a worker stops responding. Closing the application closes its owned conversion processes. An explicitly opened Digital window is independent and can remain open.

**Export package** writes a new folder containing the circuit, child circuits, identity sidecars, previews and validation report. Choose a new folder name, rather than an existing folder. **Reopen circuit package** accepts `manifest.json` or a managed output's `current.json`; it checks artifact hashes and the native circuit model. The displayed HDL coverage is the comparison recorded during conversion. Reopening does not replay that simulation or require the original source directory.

**Open in Digital** validates the selected package before opening it in Digital. If tools are missing, use **Run → Check tools and memory** for the specific dependency diagnostic. A failed conversion or a changed preview leaves the last valid displayed result available.

The commands above run from the source checkout. For the packaged application,
start `VerilogToDigital.exe` and follow [portable-windows.md](portable-windows.md),
including the example-copy and Unicode-installation instructions.

Open in Digital uses generated ASCII circuit filenames relative to the
package directory because the Windows Java launcher can alter Unicode
filename arguments. Renamed non-ASCII circuit paths produce
`DIGITAL_ARGUMENT_PATH`.
