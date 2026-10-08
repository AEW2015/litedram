# UltraScale / UltraScale+ paired DDR4 integration plan

Date: 2026-10-05. Status: proposed implementation plan; this document adds no RTL or hardware qualification.

## Intended result

Make bank-group scheduling, throughput-preserving DFI conversion and queued paired DMA available through normal LiteDRAM/LiteX configuration. Both existing `USDDRPHY` / `USPDDRPHY` and experimental `USNativeDDRPHY` should use the shared controller/frontend features when their memory geometry and DFI contract permit them. Board targets supply physical connectivity, electrical settings and qualified rate limits.

"All US / US+" means a reusable architecture for compatible external DDR4 interfaces. It does not imply every FPGA/package/bank can run 3200 MT/s, or that DDR3, PS DDR, HBM, x4 DIMMs, ECC and multiple ranks share the same implementation. Qualification must identify the exact device, speed grade, memory organization, board and toolchain.

## Evidence and current gaps

The BCU1525 x64 channel-0 paired1024 campaign passed 24 short and 60 sustained DMA cases at each requested rate. Five-run 1 GiB PRBS31 write/read medians are:

| MT/s | Write GB/s | Read GB/s | Classification |
|---:|---:|---:|---|
| 2400 | 16.641095 | 16.609354 | Normal routed timing signoff |
| 2666.667 | 18.407955 | 18.363296 | Normal routed timing signoff |
| 2933.333 | 20.232225 | 20.177218 | Experimental Native clock overclock |
| 3200 | 22.056848 | 21.986131 | Experimental Native clock overclock |

This is warm host-trained channel 0 / exposed 1 GiB, with one volatile load per full campaign. It is not stock BIOS cold-start or all-channel qualification. The exact report is [the BCU1525 campaign](../../../bcu1525_work/NATIVE_PAIRED_DMA_20261005.md).

[LiteDRAM PR 406](https://github.com/enjoy-digital/litedram/pull/406) already proposes Native PHY support, optional paired scheduling and a DMA benchmark, coordinated with LiteX and litex-boards. Its hardware evidence has its own board/profile scope; the local BCU work must remain separately attributed.

Inspection of the current local source found these integration gaps:

- `core/controller.py` explicitly restricts dual-slot operation to x32/two-BG or x64/four-BG, eight phases, one rank and 10 column bits. The ordinary four-phase interleaved mode accepts x16/x32 with two BGs only.
- `core/crossbar.py` has literal BL8 address slices for the current geometry. All masters must share the selected mapping; a DMA-only permutation would break CPU/DMA coherence.
- `frontend/paired.py` supports separate read and write adapters, sys-domain ports and full-word writes. Arbitrary AXI byte strobes are not supported by that adapter today.
- `phy/dfi.py` contains the verified full-width odd-read-latency correction. Its generic wrapper still unconditionally supplies `csr_cdc`, while the current component-mode `USDDRPHY` constructor does not accept that argument. Wrapping another PHY requires a defined CSR crossing contract.
- `test_paired_usddrphy.py` uses real USPDDRPHY settings with a behavioral PHY. That establishes digital compatibility for its tested four-phase x16 cases, not serializer calibration or routed timing on hardware.
- `phy/usnative/ddrphy.py` still has an integrated single-rank, x8-lane, fixed command/address envelope and experimental diagnostic options. `doc/usnative.md` explicitly describes the provisional x64 BIOS as reduced-width only. The new Python host-trained full-width results do not remove that firmware limitation.
- The stock generator and SoC frontend path do not yet assemble the complete BCU paired design through a single supported configuration.

## Shared design contract

Use memory and PHY capabilities to select features; avoid selecting scheduling by FPGA name alone. A normalized descriptor should cover physical DQ width, device organization/DQS grouping, BA/BG/row/column/rank geometry, burst length, physical CK per controller cycle, slots and offsets, DFI phase/data widths, latency/valid semantics, byte masks and supported training geometry. Keep source eligibility, CAD legality and hardware qualification as distinct states.

For single-rank x16/x32/x64 buses with BL8, one burst carries respectively 128/256/512 bits. A paired frontend exposes 256/512/1024 bits. A four-phase controller schedules one BL8 per cycle and alternates groups across cycles; a throughput-preserving eight-phase controller can schedule two BL8s per cycle, four physical CK apart. Enlarging a port without the corresponding command/data schedule is not a bandwidth improvement.

Retain the tested conservative slot classification, groups 0/2 versus 1/3 for four-BG memory, initially. Derive geometry from BA/BG fields rather than assuming that a physical x64 interface always means four groups. Preserve real CK timestamps, legal turnaround, refresh and rank/bank recovery. Derive supported short/long timing parameters from the memory configuration, with matching MR6 programming; reject unsupported combinations rather than silently treating every DDR4 as tCCD_S=4/tCCD_L=8.

## Implementation sequence

| Step | Deliverable | Exit condition |
|---|---|---|
| 1. Preserve and extract | Freeze existing BCU/XEM evidence; separate controller/frontend changes, converter correction, Native PHY fixes, firmware and board settings from diagnostics. | Reproducible source/tool/profile identities; no board pins, measured lane taps, VREF code 29 or experimental latency overrides become global defaults. |
| 2. Geometry and timing | Add the normalized capability descriptor and derived shared address mapping; extend the existing interleaved/dual-slot validation systematically. | x16/x32/x64 with two/four BGs and supported row/column geometries pass mapping bijection and physical-CK protocol checks; unsupported configurations fail clearly. |
| 3. Shared controller / converter | Package bank-group scheduling, owner tags, data/mask steering, refresh handling, registered timing options and the odd-latency converter fix independently of Native primitives. | Sustained adjacent commands, slot-1-only traffic, odd/even read latency, writes, turnarounds and refresh pass end-to-end checks; default DDR3/DDR4 behavior passes regressions. |
| 4. Normal frontend API | Add a core/crossbar paired-port factory and generator configuration; use the existing queued adapter, initially for separate full-word read/write streaming ports. Make queue depth configurable; 64 is the tested starting point. | Ordered data, reservation/backpressure, mixed CPU/DMA traffic, cache maintenance where needed, address boundaries, sticky faults and reset/retraining behavior work through normal interfaces. |
| 5. Both PHY backends | Enable four-phase group interleaving on USDDRPHY/USPDDRPHY; add throughput-preserving wrapping only after clock, latency and CSR CDC contracts are established. Package Native fixed scheduled pop and its pipelined monitor as a coherent profile. | Digital simulations plus real synthesis/place/route for representative US and US+ parts; fresh pin/topology/clock/VREF/CDC audits and rated timing signoff. |
| 6. General initialization | Extend LiteX BIOS/DFII packing and calibration for the actual full DFI geometry, both slots and lane count; perform training-window selection with distinct direct patterns and a sustained precheck. | Stock startup reaches controller-ready without importing a saved BCU profile; training failures keep DMA blocked; retraining clears old response epochs. |
| 7. Board qualification | Add target profiles and run the matrix below, beginning with rated clocks and one channel. | Repeatable cold starts, separate reconfiguration/software resets, full accessible capacity, retention, both-slot patterns and large DMA pass with exact timing/image/firmware evidence. |
| 8. Broader interfaces / rollout | Extend masked paired writes and AXI/other bus adapters, then qualified rank/DQS/ECC organizations; publish coordinated LiteDRAM/LiteX/litex-boards changes. | Each added capability has its own protocol and hardware evidence; normal configurations are documented and eligible defaults can be considered. |

Steps 2–4 provide the reusable performance feature. Step 5 makes it available through both US PHY styles. Step 6 is the principal remaining productization gate for the full-width Native path: the current host-trained successes do not establish autonomous initialization.

## Interface and compatibility decisions

- Keep `with_bank_group_interleaving`, `with_dual_slot` and throughput-preserving conversion opt-in initially. Validate the combination once at core construction and expose an immutable resolved configuration.
- Proposed API: a paired-port factory allocates the two child masters and returns an adapter exposing a normal `.port`, `.error` and `.drained`. It must not change controller address mapping after ordinary ports have been allocated. The exact public name should be settled during step 4.
- Preserve CPU and ordinary native ports through the same crossbar mapping. Mixed traffic must remain correct and make progress; peak sequential bandwidth is not a guarantee for random or mixed read/write traffic.
- Initially allow aligned full-word streaming writes. Extend the writer to carry per-child byte enables and verify physical mask steering before exposing arbitrary paired AXI writes. A full read/write bus also needs composition of the separate read/write paths and correct response/order semantics. Do not silently use read-modify-write without ownership/coherence rules.
- Keep normal DMA reader/writer interfaces separate from the optional PRBS/counter benchmark. Applications should gain the streaming datapath without carrying benchmark generators/checkers or diagnostic CSRs.
- Report width, slots, queue depth, address mapping, configuration identity, fault state and payload/cycle counters. Revoke admission on PHY readiness loss, calibration failure, stale firmware or sticky routing/FIFO error.
- Keep FPGA-local DDR bandwidth separate from PCIe/USB host throughput. A later PCIe DMA integration requires its own link, descriptor, outstanding-request and host-memory measurements.
- Multiple channels should instantiate independent controllers/training/admission and may use an explicit striping frontend. Four independent channel passes and aggregate bandwidth require new measurements; do not multiply channel-0 results by four.

## Verification and hardware matrix

| Axis | First implementation / hardware targets | Expansion |
|---|---|---|
| FPGA / PHY | One UltraScale component-mode design, one UltraScale+ component-mode design, Native XEM8320 x16 and BCU1525 x64 | Additional Kintex/Virtex and eligible US+ PL devices selected by exact package/bank legality; no PS-memory implication |
| Controller ratio | Existing 1:4 interleaving; Native 1:8 with full conversion | Component-mode 1:8 wrapping after CDC/clock qualification |
| Bus / geometry | Single rank, x8-lane organization, x16/x32/x64 bus, two/four BGs | Alternate row/column capacity, x4/DQS grouping, multiple ranks, x72/ECC and registered DIMMs as separate work |
| Clock | Device-, topology- and memory-rated profiles | Explicit experiments only for rates outside any rated limit |
| Traffic | Both-slot MPR/direct patterns; masked/nonpaired controls; PRBS/counter; 16 MiB/256 MiB/full capacity; mixed CPU/DMA; random backpressure and row changes | Concurrent channels and application/host DMA |
| Startup / environment | True power-cycle startup, reconfiguration, software reset, failed calibration/retraining, refresh and retention | Temperature/voltage coverage appropriate to the claimed qualification |

Use the existing controller/converter and paired tests as the starting suite, then add an independent physical-CK command scoreboard for tCCD_S/L, ACT spacing/tFAW, read/write turnarounds, precharge/write recovery, refresh and rank switching when supported. Include negative controls for early return tags, missing/duplicated slot data, halfword displacement and premature FIFO advancement—the failures found during BCU bring-up.

Run legacy suite/BIOS simulations, supported generator elaboration, and representative implementation builds. Hardware acceptance requires clean data and PHY/FIFO/paired counters, both-slot qualification and correct payload counts. Report separate write/read bandwidth, repetitions and min/median/max, including refresh and stalls. The current approximately 86% wire utilization is a BCU sequential benchmark reference, not a universal acceptance threshold.

## Review / upstream packaging

Proposed review units, each with focused tests and explicit dependencies:

1. Shared geometry/capabilities and bank-group timing/address mapping.
2. Throughput-preserving DFI conversion and latency-contract correction.
3. Paired controller ownership/data paths and frontend factory/streaming API.
4. US/USP component-mode integration and rated reference examples.
5. Native production profile, full-width BIOS initialization and ABI updates, coordinated with LiteX.
6. Board profiles, generator examples, reproducible benchmark and qualification documentation, coordinated with litex-boards.

Reuse PR 406's shared code and tests where appropriate and keep its exact evidence identities. Do not fold the experimental BCU tree wholesale into that PR or claim its tests qualify new configurations. Keep generated device queries, CAD caches, bitstreams and hardware logs outside source; publish compact manifests and reproducible commands.

The next implementation task is steps 1–2: extract the tested shared changes and define capability/geometry validation. The first cross-family hardware milestone is a rated UltraScale component-mode DDR4 paired-streaming pass, alongside an unchanged BCU reference rebuild. The first Native release milestone is stock BIOS full-width cold initialization, followed by repeatable memory/DMA passes.
