# Optional local models

Local models are optional. Ordinary conversion uses the deterministic mapper, placement and routing pipeline. A model can propose geometric changes to an already validated canvas; it cannot change component types, attributes, rotations, widths, drivers or electrical connections.

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py models status --json
.\.venv\Scripts\python.exe scripts/v2d.py models install --model qwen3-4b-q4_k_m --backend cuda --json
.\.venv\Scripts\python.exe scripts/v2d.py convert design.v -o generated --llm auto --json
.\.venv\Scripts\python.exe scripts/v2d.py convert design.v -o generated --replace --llm 4b --layout-request "Group related logic and shorten feedback paths" --json
```

Installation downloads only the pinned runtime archives and model weights into `.tools`. No Python inference framework or global Python installation is needed. `models status` checks file presence and size; `models verify` and every model load check the cryptographic artifact bindings. Runtime startup does not download missing files. A missing model produces a recorded refinement diagnostic and leaves the valid deterministic circuit selected.

`scripts/provision_llm.py` downloads models in resumable 8 MiB ranges by default (`--download-mode stream` uses one stream, as `models install` does). Ranges are written to a `.range-*` staging file beside the model, and the model is published only after its whole-file SHA-256 matches `scripts/llm-lock.json`. Recorded ranges are discarded when their staging file no longer exists or after a whole-file hash mismatch, so a rerun downloads every range again.

The runtime is the C++ `llama-server` from llama.cpp stable `v0.4.0`, whose release points to nightly `b10809`, actual commit `5266f24da75dc449bd56cbed7addb9c8e4a6a73e`. Exact archive/model URLs, revisions, sizes and SHA-256 values are in `scripts/llm-lock.json`. The chosen Windows GPU build uses CUDA 12.4 and app-local runtime DLLs. Primary sources are the [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases/tag/v0.4.0), [pinned server implementation](https://github.com/ggml-org/llama.cpp/tree/5266f24da75dc449bd56cbed7addb9c8e4a6a73e/tools/server), [official Qwen3-4B GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF) and [official Qwen3-14B GGUF](https://huggingface.co/Qwen/Qwen3-14B-GGUF).

One owned local server handles one inference slot. It listens on loopback with an ephemeral port and a per-process API key. The selected GPU is the `nvidia-smi` index, which follows PCI bus order; the server receives that index with `CUDA_DEVICE_ORDER=PCI_BUS_ID`, so CUDA loads on the device that admission measured. The app supplies only a bounded JSON schema request, with no agent tools or shell/filesystem authority. Model output is parsed again locally with duplicate-field rejection, strict action validation and a snapshot hash. Source labels and diagnostic strings remain untrusted data. Prompts contain selected verified geometry, complete boundary endpoints for the included nets, native metrics and explicit counts of omitted information. They contain no source filesystem paths by default. The runtime tokenizes the formatted chat before generation, leaving room for the bounded completion; context overflow can narrow the next prompt within the same attempt limit.

Implemented actions are:

| Action | Effect | Bounds |
| --- | --- | --- |
| `move_component` | Translate one existing component | Delta on the 20-unit grid, at most 2000 units per axis |
| `move_group` | Apply the same translation to existing components | At most 16 IDs, each moved once per batch |
| `reroute_nets` | Prioritize specified existing nets and choose deterministic route order | At most 32 IDs; the whole canvas is rerouted |

The response also contains the exact current candidate hash and a short explanation. There are at most eight actions per response. Coordinates must stay within Digital's bounds. Actions cannot write raw wires or silently resolve placement collisions by moving other components.

The selection loop emits each candidate into a private directory, reloads it in a separate Digital adapter, verifies component/bit/net identities, serialization order, geometry and native model construction, and binds the result to exact circuit and sidecar hashes. It accepts a candidate only when native area, wire length, bends and crossings are all nonincreasing and at least one decreases. Failed, worse, equal, malformed, stale and timed-out proposals retain the current verified layout. The selected circuit subsequently runs through the normal original-Verilog behavior comparison before publication.

The default refinement budget is three proposals and 120 seconds across all hierarchy canvases. Use `--refinement-attempts` and `--refinement-budget` to adjust these within their supported bounds. Model startup, requests, candidate rerouting and candidate validation consume that budget; the job supervisor separately enforces its full conversion deadline and committed-memory limit. The server closes before final behavior validation. No model is kept resident across jobs.

Advanced settings expose `--model-context-tokens` (1024–8192; automatic when omitted), `--model-memory-mib` (64–12288; default 6144) and `--model-idle-seconds` (1–300; default 30). The corresponding project refinement keys are `context_tokens`, `model_memory_mib` and `idle_seconds`. An explicit CLI model control enables local inference while preserving a saved model choice; contradictory explicit off/replay controls are rejected. Saved context settings also bound replay prompts, so recorded action scopes can be reproduced without starting a model. Memory and idle settings remain inactive during replay.

The model's nested memory ceiling must fit within the outer job and any per-process ceiling. These ceilings are not summed as if their full amounts were allocated: the ordinary model and outer job can both have a 6144 MiB ceiling, and the outer limit covers all processes together. A larger model profile needs explicit matching limits. Invalid limits fail before a job starts or a published result changes.

Hierarchy refinement happens before a child circuit's hash enters its parent's dependency catalog. Each attempt receives an immutable, checked copy of the registered child files. Component serialization order is preserved because Digital uses it to order custom component pins.

Visible wires remain the default. With `--tunnels`, each canvas is first validated with named tunnels exactly as without refinement; that canvas fixes which long nets become tunnels and their names. Refinement then works on the separately validated visible-wire canvas, because rerouting does not preserve separate physical tunnel branches. After an accepted proposal, the same tunnels are inserted into the selected canvas, which must pass the same native checks. If insertion or validation fails, the validated refined visible-wire canvas is published with fallback reason `native_tunnel_validation` and a `TUNNEL_FALLBACK` report diagnostic; the canvas's `tunnels` entry names the failure and any planned tunnel without a clear position. A compacted canvas often has less room for the planned tunnels. Without an accepted proposal, the validated tunneled canvas is published unchanged; it is also published unrefined, with a `TUNNEL_CANVAS_UNREFINED` diagnostic, if the visible-wire canvas cannot be validated. A tunneled canvas that already fails before refinement uses the existing visible-wire fallback, and those visible wires are refined.

The current strict Pareto selection policy favors compact layouts. A free-form request for wider spacing cannot authorize a larger-area candidate under that policy. Use the explicit `--spacing` setting to establish the desired spacing before optimization. Wider-spacing or grouping preferences with different quality tradeoffs require a separately defined selection policy.

Reports identify each canvas by its top label and, for hierarchy, its stable module ID. They contain the baseline strategy, baseline metrics, selected metrics, proposed actions, raw responses, rejection reasons, exact runtime/model configuration and measurements. A rerouted canvas reports strategy `constrained-orthogonal-v1` and keeps the fallback and tunnel decisions of the canvas it came from. For a tunnel canvas, the baseline and selected metrics describe visible wires; `tunnels` records `refined_canvas: visible_wires`, the planned tunnels and an outcome (`inserted_after_refinement`, `baseline_retained`, `visible_fallback` or `not_refined`), and `published_metrics` describes the published canvas. Model explanations are suggestions, not evidence of electrical correctness. `baseline-preview.png` allows comparison with the canvas before refinement and contains the same components as the published circuit: the validated tunneled canvas when tunnels are published, otherwise the validated visible-wire canvas. In hierarchy this is the initial top canvas after its child specializations have been prepared; it is not a second untouched copy of the whole original package.

Recorded action responses can be replayed without loading a model:

```json
{"schema_version": 1, "responses": ["{\"schema_version\":1,\"candidate_id\":\"...\",\"actions\":[],\"explanation\":\"No justified change\"}"]}
```

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py convert design.v -o replayed --replay-actions responses.json --json
```

Use the actual raw responses from the recorded run; the abbreviated hash above is not valid. Replay uses the same snapshot and native checks. Source/runtime differences that change the mapped canvas make a proposal stale. Conversion caching is disabled whenever refinement is enabled, so a local-model request is never silently replaced by a cached proposal.

Full result freshness validation checks the recorded model/runtime files as well as converter/source/artifact bindings. `validate --artifacts-only` verifies a portable result without requiring the original model installation and explicitly reports that narrower scope. Neither mode claims to rerun model inference or replay the original behavior tests.

## Larger models and memory recovery

The 14B profile uses 2048 context tokens, an 11264 MiB model limit and a
12288 MiB whole-job limit. Select the desktop preset or pass
`--model-context-tokens 2048 --model-memory-mib 11264 --memory-mib 12288`
with `--llm 14b`. Loading still depends on available RAM and VRAM.

An admission-memory failure or recognized native allocation failure permits
one smaller-profile retry within the existing time and attempt limits. The
runtime closes the failed server, checks memory again and can reduce the Q4
full-GPU q8 context from 4096 to 2048 tokens. Model selection, GPU and memory
ceilings stay unchanged. Other failures retain the best validated circuit.
