# Sequential conversion

Synchronous conversion uses Digital's native `D_FF` components with readable datapath and mux components. Auto profile selection chooses `sequential-v1` when the lowered design retains registers. Every register must use the same rising-edge clock, driven by a dedicated scalar top-level input. Reset and enable priority follows the original HDL through the lowered mux logic.

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py convert tests/fixtures/verilog/sequential/counter.v -o .runs/my-counter
```

State must be established by an input sequence. The initialization analysis starts every retained register bit unknown, snapshots all register inputs together at each edge, and tracks which bits become known. It uses conservative bit knowledge, including selected reset/load muxes and controlling gate values. The conversion proceeds only when every retained register bit is known. Digital's native default zero is disclosed as a simulator detail and is never treated as an HDL initialization assumption.

The default sequence tries all-zero inputs, then all-one inputs, then zeros again. Each input phase repeats until all state is known or the abstract state stops changing, with at most 256 total cycles. This can reset a counter, enable a register load or flush a shift register. Designs that need another startup protocol can supply it using `--trace path/to/trace.json`. A bare counter without reset/load and hidden uninitialized state are rejected.

An explicit trace for a clocked eight-bit D register is:

```json
{
  "schema_version": 1,
  "clock": {"port": "clk", "edge": "rising"},
  "initialization": {
    "kind": "input_sequence",
    "cycles": [{"values": {"d": 17}}]
  },
  "cycles": [
    {"values": {"d": 128}},
    {"values": {"d": 255}}
  ]
}
```

Every cycle supplies every nonclock input exactly once, using unsigned integer bit patterns within its declared width. Clock transitions are implicit. Prefix length is 1 through 256; checked length is 1 through 1024. Input files are bounded to 1 MiB. Without an explicit trace, directed boundary and repeated-input cycles are followed by seeded random cycles. For sequential designs `--random-vectors` selects 0 through 512 random cycles (default 256), and a generated trace checks at most 1024 cycles. Repetition is intentional because temporal behavior depends on prior cycles.

Icarus compiles the unchanged original sources, starts registers in their normal unknown state, and applies the same input prefix. It never forces or assigns internal registers. Checked cycles are sampled before the rising edge, after rising-edge updates settle, and after the falling edge. Digital returns observations in the same order. Wrong values, high impedance, unknown reference outputs, missing samples or reordered phases prevent publication.

The report records the complete initialization prefix, the proof's known register/bit counts, each observation phase, checked cycle count and simulation assumptions. Temporal coverage is sampled; passing these traces is not a proof over all possible states and input histories. The usual reloaded component, topology, splitter bit and geometry checks remain mandatory.

Feedback routes treat registers as dependency boundaries. Compact ELK routing and reserved fallback both support register feedback. Optional long-net tunnels prioritize clock distribution and are checked through Digital's native net list. Unused complementary register outputs retain explicit isolated bit identities without adding unnecessary splitter symbols.

The sequential profile supports widths 1 through 64 and at most 250 mapped components including helpers. Ordinary `case` selection uses Yosys `pmuxtree` lowering. Falling-edge registers, asynchronous reset, latches, multiple/gated clocks, clock-as-data and source initializers remain unsupported. Source inputs that use the clock as data are rejected because the current event protocol requires data to settle independently while the clock is low.
