> This is an index of original workspace paths and outcomes. The compact overlay includes selected source and small evidence records only. Large build closures, reports and original artifact manifests are intentionally omitted; see README.md for the verified clean controller replay and scope.

# Agilex 5 fabric DDR bank 3B handoff

This handoff concerns the two FPGA-fabric DDR interfaces on the Agilex 5 E-Series 065B Modular ES board. Bank 2B is logical bank 0; bank 3B is logical bank 1. Each selected x32 interface addresses 8 GiB. The current design leaves bank 3B's ECC byte disconnected. Qwen weights, model execution and runtime transport are outside this handoff.

Paths below are relative to the original project source root, `agilex5-litedram-phylite`. The publication's source manifest and companion repository guide establish their destination paths and exact revisions. Do not use an older single-bank DDR3200 top-level README as the status of this dual-bank work.

## Outcomes to preserve

| Profile | Measured or recorded outcome | Qualification boundary |
| --- | --- | --- |
| DDR1600, native/reference 200 MHz, CK 800 MHz, CL12/CWL11 | Restore10 passed strict diagnostics on both 8-GiB banks, full-capacity read/write, sparse/isolation and concurrent DMA. Default A5 tap 5, alignment 1; ABS/DMA fixtures passed. | Nominal functional hardware evidence. External DDR I/O, full CDC/signoff and PVT qualification are not established. |
| DDR2133.333, native/reference 266.666667 MHz, CK 1066.666667 MHz, CL16/CWL11 | Memory v14 compiled with zero errors and 48 warnings and passed its scoped internal implementation audit. Hardware remains negative: FW14 passed bank 0 local diagnostic stages, then bank 1 initial MPR had six mismatches. | A passing internal STA audit is not a passing DDR read/calibration result. No strict dual-bank DDR2133 pass. |
| DDR2666.667, native 333.333333 MHz, reference 266.666667 MHz, CK 1333.333333 MHz, CL20/CWL14 | Dense v1 has sealed source, not a completed fit. The proposed dense v2 build does not exist; preparation recipes are unexecuted. Latest dense-repair-v4 controller has source/simulation evidence only. Earlier manual DDR2666 experiments failed bank 1. | `EXPERIMENTAL_LEGACY_NOT_VENDOR_SUPPORTED`; no new dual-bank fit or hardware qualification. |

Restore10 uses `build/dual_ddr1600_opentpu_native_v1/memory_test.sof` (SHA-256 `8e30596259bcab6f772f1a140eea71da5d233225b3ecc078c1c3ac75ceaaa06f`) and `build/dual-ddr-opentpu-native1600-fw-v2/dual_ddr_diagnostic.elf` (`aee68cdbb688e77d5737c956999a3ce4149c462f9f3434c5fa4897bf322b9dd4`). This image contains accelerator integration, but its memory diagnostic evidence does not require model weights/runtime. It is a preserved memory baseline, not a claim about which image is live now.

DDR2133 v14's selected controller SHA-256 is `46d487437f4631bfb742e6df77e8a864ce6486f4b7be33d225faf46386c976fb`; its base/frontend latency is 25/27 with alignment 1. Its 3,076-input manifest SHA-256 is `65c5cff9cfa0cf312c43d5b487937a530b395a24127cd6faf6ea1d498698a4b3`; SOF SHA-256 is `9d52885ca910d0de0f957dc512b6960faaebefc1c59e5125fbec6e727e6c58c4`.

## Primary evidence

All following filenames are under `integration/dual-ddr-v1/` unless stated otherwise. Prefer these immutable result records over historical status paragraphs in mutable documents.

Passing DDR1600:

- `results/hardware-native1600-restore10.json`
- `results/hardware-native1600-restore10-strict-validator.json`
- `results/hardware-native1600-restore10-fixtures.json`
- `results/hardware-native1600-restore10-board-state.json`
- `results/native1600-restore10-post-program-source-verification.json`
- `results/native1600-restore10-clock-profile-verification.json`
- `results/native1600-restore10-recorder-summary.json`
- `metadata/firmware-native1600-opentpu-v2-dependency-review-r2.json`

DDR2133 v14:

- `results/native2133-memory-v14-root-compile-terminal-r1.json`
- `results/native2133-memory-v14-independent-source-audit-r1.json`
- `results/native2133-memory-v14-independent-audit-r1.json`
- `results/hardware-native2133-v14-memory1-negative-root-r1.json`
- `results/hardware-native2133-v14-memory1-negative-review-r1.json`
- `results/hardware-native2133-v14-fw14-bank0first-run1.json`
- `results/native2133-v14-fw14-independent-hardware-interpretation-r1.json`
- `results/native2133-v14-MPR-byte-lane-binding-amendment-r1.json`
- `results/native2133-v14-MPR-startup-A5-source-review-r1.json`
- FW8–FW13 diagnostic records named `results/hardware-native2133-v14-fw*-run1.json`, including startup, telemetry, snapshot, FIFO, reset and DQS trials.

DDR2666:

- `results/native2666-phy-pair-generation-failure-v1.json`
- `results/native2666-memory-dense-source-selection-r1.json`
- `results/native2666-dense-return-timing-manual-v2-author-assessment-r1.json`
- `results/native2666-legacy-phy-transformed-independent-peer-r1.json`
- `results/native2666-manual-abi-peer-r1/peer_review.json`
- `phy-legacy2666-transformed-v1/pair-manifest.json`
- Historical manual diagnostic evidence: `hardware-fw28-review-v1/assessment-strict.json` and `assessment-read-recomputed.json`. FW28 had zero passing bank-1 tuples over 565 points, 9,040 data-word errors and 16 baseline errors. One malformed UART summary line means strict capture integrity failed; raw-read recomputation is a separate result, not a repaired capture or calibration pass.

## Source roots and exact build closure

The source manifest is authoritative. Keep the consumed files and their hashes, including files that happen to live under `build/`; do not substitute a nearby version because its name looks newer.

Passing native1600 recipe and generated artifacts:

- `integration/dual-ddr-v1/controller-native1600-v1/`: `generate.py`, `phy_native1600_v1.py`, `validity_patch.py`, reset-distribution helpers, `generated-r4/` and selected `memory/` or `full/` constraints.
- `integration/dual-ddr-v1/ddr1600-diag-v1/phy-bank0/` and `phy-bank1/` generated PHY closure.
- The selected build's QSF/QPF, `quartus.ini`, public SDC, controller Verilog and `dual_ddr_inputs.json`. Memory-only and integrated builds have different top-level closures; restore10 evidence binds the integrated build above.

DDR2133 v14:

- `integration/dual-ddr-v1/controller-native2133-dense-repair-v5/`: generator, `dense_phy.py`, `dense_crossbar.py`, `dense_mux.py`, selected generated RTL/profile and source/simulation proof records.
- Parent `controller-native2133-fresh-v1/` and native1600/V8 generator dependencies.
- `integration/dual-ddr-v1/native2133-memory-v1/draft/` top, bank paths and CSR router; shared `rtl/dual_ddr_cal_service.sv` and `rtl-r7/dual_ddr_control_system_r7.v`.
- Bank 0 PHY rooted at `phy-native2133-v1/generated/bank0/phy_probe_phy/`; corrected bank 1 PHY at `phy-native2133-bank3-r3/generated/bank1/dual3_phy_probe_phy/`, with `selected-pair-manifest-r3.json`.
- `integration/dual-ddr-v1/config-live-bits-review-v1/dual_ddr_native2133_config_live_bits_diagnostic_v1.sdc`.
- `build/dual_ddr2133_memory_v14/` direct source files, `dual_ddr_inputs.json` and `candidate_seal_r14.json`; `quartus-native2133-memory-v14/helpers/` read-only audit and site queries.

DDR2666 development source:

- Latest `integration/dual-ddr-v1/controller-native2666-dense-repair-v4/` and parent `controller-native2666-fresh-v1/`.
- Legacy generated pair `phy-legacy2666-transformed-v1/` and its manifest; preserve fresh-generation rejection records.
- `prepare_and_seal_native2666_memory_dense_v2_r3.py` is a proposed, unexecuted preparation recipe. Do not report its output directory as an existing candidate.

Shared generation closure includes consumed `litedram_phy/` modules, `config/board.json`, pinned Migen/LiteX/LiteDRAM sources, and these easily missed source inputs:

- `integration/opentpu-v1/controller/generate_external.py`
- `integration/opentpu-v1/controller-v8-ddr2666/generate_external_v8_ddr2666.py`, `segmented_timer_v8.py`, `fullrate_dma_v5.py`
- `build/dma-fullrate-3200-timing-v231-physical-eco-v81/generator_snapshot.py`
- `build/dual_ddr_opentpu_v11/litedram_external_controller.v`
- `integration/dual-ddr-v1/controller-fast-v11/changes.json`

The last two supply reset mappings to dense `layer_repairs.py`; the older generator snapshot is read as Python source. Imported-module dependency maps alone can miss files loaded with `read_text()` or `exec`. A reproducible source closure must include both kinds. QSF references also consume Nios/Qsys source under `build/hardware-core400-pipelined-v1/`; retain the referenced QIP/HDL/SDC closure, not Quartus databases. Diagnostic firmware and its BSP/header dependencies must match the controller profile and CSR ABI.

## Reproduction boundaries

Controller entrypoints are the selected `generate.py --output NEW_DIRECTORY` recipes. Native1600 additionally applies the selected `replicate_reset_r4.py` transformation. Dense generation performs its layer repairs itself. Python source/dependency maps record the original Python 3.12 environment; preserve or explicitly reconcile those dependency versions and path mappings.

Historical candidate preparation scripts are provenance, not a proven clean-clone workflow. They refuse existing output directories and require parent builds, seals, review hashes and firmware records. No claim is made that a historical full preparation chain was rerun from a clean checkout for this publication.

A fresh synthesis/fit can instead begin from the published selected QSF/QPF/controller/SDC source set and its recursively resolved generated-IP closure, with relative paths reconciled by the companion guide. In a new candidate directory, the compile entrypoint is:

```sh
quartus_sh --flow compile memory_test
```

Use the recorded Quartus Pro 26.1.1 tool version and candidate settings. This command is documentation, not an action performed by this handoff. After fitting, use `quartus-native2133-memory-v14/helpers/audit_readonly_v14.tcl` and `sites_readonly_v14.tcl` with paths directed to the new candidate and new output directory. The companion guide adds clone/setup commands and exact publication layout. Never run a prepare, compile or report command into a preserved evidence directory.

## Next investigation

1. Preserve DDR1600 as the control. Confirm both banks' pin/lane/group mapping and reference clocks against `reports/DUAL_DDR2666_BOARD_INVENTORY.md`. Bank 2B reference is BR49/BU49 from Si5332 OUT7; bank 3B is AC68/AC72 from OUT8. Default schematic/startup clock labels are not evidence of the active experimental clock profile.
2. Begin with the v14 bank-1 initial MPR failure, not the already repaired v12/v13 setup failures. The affected logical byte maps to DQ8–DQ15, group 1/lane 4. Compare actual RX capture ordering, lane-valid alignment, FIFO/reset/DQS behavior and bank-specific generated metadata. FW14's bank0-first result is a useful control, not evidence that bank1 calibrated.
3. Respect the model boundary: FW11 repeated-capture simulation covered 60 alternating frames and 480 raw CSR words without a digital reproducer, but excluded the vendor hard RX FIFO. Dynamic receiver-duration mapping has not been established. These facts neither prove nor exclude an electrical/hard-PHY cause.
4. Separate any physical timing change from sample/order or training changes. Preserve exact read latency, alignment and CSR/firmware contracts or update them together with proofs. Earlier v4 positive timing omitted bank0 forwarded-clock coverage because bank1 clock names overwrote it; corrected namespace/lane checks must remain in force.
5. For DDR2666, resolve the vendor support boundary explicitly. Fresh generation rejects CK1333.333 MHz above its 150–1250 MHz range; a legacy transform does not remove that restriction. Do not qualify the higher rate by relaxing SDC or borrowing a lower-rate calibration result.

## Acceptance and hardware ownership

A source handoff is accepted when a fresh checkout resolves every consumed source/IP/firmware dependency, verifies selected hashes, and can generate and compile in new directories without historical caches. Generation should match the published RTL hash or document and prove intentional differences.

A repaired implementation must pass all applicable internal timing corners and setup/hold/recovery/removal/pulse-width checks, preserve real clock coverage for both PHYs, and classify CDC/reset/unconstrained findings without blanket waivers. V14's scoped audit retains invalid generated-PHY/debug exception findings and does not establish external interface signoff; report those limits explicitly.

A DDR2133 functional pass additionally requires fresh matching firmware, observed clocks, successful MPR and calibration on both banks, strict full-capacity/sparse/concurrent memory diagnostics, zero mismatches, and source-bound raw logs accepted by the strict validator. Restore a known safe configuration and record final state as part of any authorized hardware session. A nominal pass remains distinct from external DDR electrical timing and PVT qualification.

**This handoff grants no live hardware ownership.** Another system must not program the FPGA, change Si5332 clocks, download firmware, reset or calibrate either PHY, or access JTAG/UART against the live board while this chat owns the session. Obtain explicit coordination and transfer of hardware ownership through the user/live chat first. Offline source analysis, simulation and isolated compilation are the intended starting work. The evidence describes historical configurations and must not be treated as a live-board snapshot.
