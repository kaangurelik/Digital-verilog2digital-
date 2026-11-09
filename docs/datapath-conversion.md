# Datapath conversion

Convert a combinational datapath into a new output directory:

```powershell
.\.venv\Scripts\python.exe scripts/v2d.py convert tests/fixtures/verilog/datapath/add_sub8.v --output .runs/my-add-sub --seed 42 --random-vectors 256
```

`--profile auto` preserves the scalar profile for compatible designs and selects `datapath-v1` for wider signals or arithmetic. `--profile scalar-v1` enforces the one-bit boundary. `--profile datapath-v1` selects the datapath mapper. Source ordering, top selection, includes, defines, parameters and output revisions follow [the scalar CLI](scalar-conversion.md).

The supported native pin width is 1 through 64. The current bound is 250 native components including helpers, 20,000 wire segments, and at least one output port. A design that exceeds mapping, routing, protocol or preview bounds fails with diagnostics and does not publish a ready result. The default compact layout and reserved fallback are described in [the user guide](../USER_GUIDE.md#supported-designs).

| Verilog behavior | Digital implementation |
| --- | --- |
| Bus ports, aliases, slices, concatenation, repeated bits and reversed ranges | One shared bus per unchanged vector; explicit scalar bit identities and native Splitter merge/split helpers for regrouped bits |
| Constants and signed/unsigned extension or truncation | Explicit constant/sign-bit connections and selected bit positions |
| Bitwise AND, OR, XOR, XNOR, NOT | Native vector gates |
| Addition, subtraction, unary negation | Native Add/Sub blocks with explicit carry/borrow inputs and unused outputs |
| Conditional selection | Native two-input Multiplexer blocks |
| Equality and ordered comparisons | Native Comparator with explicit signedness, plus inversion where required |
| Logical operators and reductions | Native gates operating on the required operand bits |
| Left/right shifts and signed arithmetic right shift | Native BarrelShifter with explicit operand sizing and oversized-amount handling |

Multiplication, division, remainder, exponentiation and general Yosys shift cells are not advertised by this profile. Yosys lowers parallel selection into mux trees before mapping; unsupported remaining cells produce a mapping diagnostic. Registers and clocks use the separate [sequential profile](sequential-conversion.md). The frontend continues to reject unsupported four-state/timed behavior; binary-safe equality handling does not introduce four-state equivalence claims.

Wide native pins that carry the same LSB-first bit-identity vector share one physical bus, for example an Add result feeding a Comparator and an output port, or a register value forwarded unchanged. A bus gets a splitter linking its positions to canonical scalar nets only when individual bits are still needed: slices, permutations, repeated bits, extension or truncation, constants, or mixed scalar use. Every identity vector has exactly one bus with at most one splitter, so permutations and width conversions remain inspectable. Before layout, the mapping check rejects a duplicated vector bus, a second splitter, a bus without native component pins, and any bit that occurs on more than one carrier without reaching its canonical scalar net through a splitter.

`work/mapped.json` records the native descriptors, physical nets, LSB-first `bits` vectors and independent `pin_bindings`. After serialization, the bit checker rebuilds connections using native wire groups and reloaded splitter offsets. It rejects connections between different identities, including a shared-bus pin whose identity, order, width or constant value differs, as well as disconnected occurrences of an ordinary design bit, missing or extra drivers, inconsistent widths, and incorrect constant values. Existing checks still compare complete pin partitions, native component attributes, geometry and Digital model construction.

Interfaces with at most 12 total input bits receive all binary combinations. Larger interfaces receive bounded directed patterns and seeded random vectors. Directed patterns cover zero/ones, signed boundaries, alternating bits, individual high/low bits and carry chains. `--random-vectors` accepts 0 through 2048; every plan contains at most 4096 vectors. The report records the seed, actual vector counts, any directed truncation, total input bits and whether coverage was exhaustive. Sampling is not a formal equivalence proof.

Icarus compiles the original HDL and represents each input/output as an unsigned bit pattern of its declared width. Signed arithmetic remains the HDL simulator's responsibility. Digital receives the same patterns as hexadecimal values; Python checks every output bit, including bit 63, and rejects high-impedance outputs or missing samples.

The published artifact set matches the [scalar outputs](scalar-conversion.md): `circuit.dig`, its identity sidecar, PNG/SVG previews with `preview-metadata.json`, `validation.json` and the hashed manifest, plus `baseline-preview.png` only when layout refinement ran. The validation report includes a separate `bits` result. Existing results are preserved, and source or validated circuit changes still invalidate publication.
