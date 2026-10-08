# USNativeDDRPHY timing closure plan

## Goal and evidence

Close routed setup, hold, and pulse-width timing for USNativeDDRPHY DDR4
profiles from 2400 through 3200 MT/s where the selected FPGA and speed grade
permit it. Preserve correct calibration, memory contents, and throughput.
Report any rate blocked by native primitive or clock limits as unsupported for
that device rather than hiding the violation with altered clock constraints.

The AES-KU40 x32 DDR4-2400 diagnostic build (Vivado 2025.2, KU040 speed
grade -1, 300 MHz 1:4 controller) passed three full calibration runs, three
BIOS 2 MiB memory tests, and a separate 32 MiB CPU memory test. It still has
setup WNS -0.424 ns, hold WHS +0.030 ns, and pulse-width WPWS 0.000 ns.
Its worst setup path crosses bank-machine control; a targeted native
read-FIFO path to `FIFO_RD_EN` has -0.335 ns setup slack. The FIFO path is
used during software-driven calibration reads, while bank-machine paths
primarily affect traffic after DFI control returns to the controller.
The passing tests do not qualify the image while these paths violate timing.
The local board investigation and raw reports are in
`litex-boards/docs/avnet_aesku40_timing.md` and `C:/ai_tmp/aesku40_timing_v20_extended_tx_scan/`.

## Work sequence

### 1. Make the timing baseline reproducible

- Save exact source revisions, part, speed grade, tool version, clocks,
  constraints, build options, firmware hash, and bitstream hash for each build.
- Report the worst and total setup, hold, and pulse-width slack separately.
  Also report the top paths ending at native `FIFO_RD_EN`, DFI payload and
  valid registers, bank-machine state, refresher/arbitration, and PHY control.
- Compare normal and debug builds, with and without the optional DMA frontend.
  Do not mix firmware changes with RTL timing comparisons.
- Check every 2400/2666.667/2933.333/3200 clock against the actual device's
  native primitive and clocking specifications before building. Keep actual
  clock constraints and label overclock experiments explicitly.

Exit: every failing path is assigned to a block and the 2400 baseline can
be rebuilt with matching reports.

### 2. Localize native FIFO draining by byte lane

The current shared `FIFO_EMPTY` reduction crosses the physical byte banks
before driving native `FIFO_RD_EN`. Replace it with one local receive agent
per byte lane. Each agent must decide its native FIFO read locally, reserve
space for reads already in flight, and capture the returning 64-bit lane item.
Join the lane items by burst sequence only after all required lanes arrive.

`NativeReadAssembler` in `litedram/phy/usnative/elastic.py` already provides
ordered lane buffering, but its input handshake is not a native FIFO read
permission. Add the adapter and outstanding-read accounting around it. The
registered mode in `NativeFIFORead` currently inserts an idle cycle after
each drain; it cannot simply be enabled for sustained full-rate traffic.
Reset and retraining must flush buffered items and cancel late responses from
the previous training epoch. Bound storage so no byte lane can overflow during
back-to-back BL8 reads or a transient lane skew.

Exit: simulations cover skewed lane arrivals, continuous bursts, adjacent
commands, empty transitions, backpressure, reset, and retraining without
loss, duplication, or byte misalignment. The final routed FIFO path has
non-negative slack at 2400, and hardware calibration plus memory tests pass.

### 3. Define and pipeline the PHY-to-DFI timing contract

Register command, write data, mask, and DQS-related control at explicit
boundaries. Register read assembly and read-valid output. Update PHY read and
write latency settings together with the pipeline so the controller sees the
same command-to-data relationship on every burst. Keep calibration's direct
DFII path aligned with the new receive latency and flush rules.

Use simulation to compare the command at the DDR pins, write data and masks,
native FIFO return, assembled word, and DFI read-valid across burst edges.
Check initialization, a single read, consecutive reads, alternating reads and
writes, and retraining after traffic. Measure additional latency and sustained
bandwidth rather than assuming the pipeline is free.

Exit: cycle-level tests pass, the critical DFI paths meet timing, and
calibration and CPU/DMA integrity tests pass on hardware.

### 4. Move control and observability off fast data paths

Register delay-count and status snapshots before CSR readback. Use explicit
request/acknowledge handshakes for tap changes, reset, and training controls;
keep broad CSR decode and optional debug selection away from 300-400 MHz
payload and native FIFO signals. Verify control-domain crossings and reset
release with simulation and timing reports.

Exit: normal and debug builds meet the same control-path timing requirement,
and calibration still reports accurate tap, FIFO, and failure status.

### 5. Shorten controller feedback without changing command timing

After the PHY paths are localized, fix bank-machine, refresher, and command
arbitration paths. Register decisions only where the issued command and its
JEDEC timing counter advance together. Do not start a bank timer merely when
a command enters a queue. Prove ordering, backpressure, refresh priority,
and tRCD/tRP/tRAS/tRC/tCCD constraints in simulation.

Exit: routed bank-machine and arbitration setup slack is non-negative at the
selected controller clock. Existing controller and refresh tests pass, and
hardware memory integrity is unchanged.

### 6. Complete the optional 1:8 controller path

Keep the native serializer and BITSLICE clocks required by the DDR rate while
halving the controller clock relative to 1:4. The current one-command-per-word
controller needs one physical BL8 per transaction: eight 32-bit DFI phases for
x32 memory, or eight 16-bit phases for x16. At a 150 MHz x32 controller clock,
that interface can issue at most one 32-byte burst per slow cycle, so its
nominal maximum is half the 1:4 raw bandwidth. Full-rate 1:8 requires an
explicit second burst command plus a 512-bit word assembly contract; merely
exposing eight 64-bit phases loses half the data. Keep the CPU on its own
clock and validate the reduced-width converter, clock crossings, calibration
ownership, and read latency before pursuing that wider architecture.

| DDR rate | 1:4 controller | 1:8 controller |
| ---: | ---: | ---: |
| 2400 MT/s | 300 MHz | 150 MHz |
| 2666.667 MT/s | 333.333 MHz | 166.667 MHz |
| 2933.333 MT/s | 366.667 MHz | 183.333 MHz |
| 3200 MT/s | 400 MHz | 200 MHz |

Exit: 1:8 simulation and hardware integrity pass; routed timing closes;
sequential and interleaved DMA results quantify the reduced-width controller's
throughput. A lower controller clock does not waive native
BITSLICE pulse-width limits.

### 7. Tune physical implementation after RTL paths are shorter

Compare a small set of Vivado placement, physical optimization, and routing
directives from the same synthesized checkpoint. Use `report_design_analysis`,
high-fanout reports, and routed path delay to select experiments. Consider a
small pblock for controller arbitration only if the reports show a placement
problem. Preserve board pin and native byte-bank mapping from the Vivado
query. Any RapidWright experiment must return to Vivado for legality, route,
and final signoff on the selected part.

Exit: retain a directive or constraint only when final routed setup, hold,
pulse-width slack and hardware behavior improve on the exact same design.

## Qualification matrix and stop rules

### 2026-09-26 audit correction

The detailed audit is in the workspace's
`hssio_experimental/timing_work/AESKU40_2400_AUDIT.md`. Reopened checkpoints
show two separate 2400 bottlenecks: r2 1:4 bank arbitration (-0.380 ns) and
common native FIFO control (-0.366 ns). In r8d 1:8 debug, the FIFO path is
-0.895 ns and trace/CSR paths also fail. The native FIFO remains at 300 MHz
despite the 150 MHz controller.

A directed simulation additionally proves that `preserve_throughput=True`
currently exposes a 512-bit x32 DDR4 controller word but emits only one BL8
READ per accepted request, transferring 256 physical bits. Two requests emit
two READs at columns 0 and 8. The recent phase-order correction is incomplete:
it does not add the missing command or fix transaction geometry. This is a
functional blocker for the experimental wide 1:8 target, independent of STA
and calibration. Correct the transaction width first (eight 32-bit phases),
or implement explicit two-burst command splitting/reassembly. The reduced
width comparison verifies command count only; it still needs full PHY/BIOS
validation. This takes priority over the TX retry proposed below.

For native FIFO retiming, use AMD UG571's shared registered-enable topology
as an experiment and verify it with the actual primitive under burst DQS.
The existing ideal queue tests neither prove burst safety nor disprove the
documented optional register. Carry any latency changes through fixed DFI
read-valid and calibration. Then fix arbitration and debug/configuration
paths before physical optimization and renewed hardware qualification.

Current AES-KU40 work is tracked in
`hssio_experimental/timing_work/README.md` (from the workspace root), with
hashed build manifests and UART evidence under `C:/ai_tmp/`. The retimed
2400 MT/s 1:4 build passes repeated calibration and CPU memory tests but
still has -0.380 ns setup slack. A full 2400 MT/s 1:8 build has -0.324 ns
setup slack and fails CK window training, so it has not reached memory tests.
The 2666.667 MT/s attempt is blocked at bitstream DRC by a 1333.333 MHz
PLL VCO outside the KU040-1 range. Keep higher-rate builds on this board
separate from a claim of timing closure or hardware qualification.

The first full x32 1:8 firmware run found CK phase 1/gate 3 and four clean
RX lane windows, then stopped because the TX scan ended at tap 176 while
lane 0 was still inside its eye. The x32 scan now extends to tap 240 without
weakening its width or center checks; the first normal 1:8 rebuild failed
earlier in CK calibration. A one-hot command-chooser experiment worsened 1:4
WNS from -0.380 to -0.559 ns and was reverted. The native FIFO_EMPTY to
FIFO_RD_EN path remains the central 1:8 timing problem. The lane-local
receive adapter is not yet integrated because its variable output-valid
timing does not meet the fixed DFI read-valid contract.

The normal 1:8 rebuild then failed the CK search before reaching TX
calibration: its best phase/gate had one clean sample, while the debug image
had ten. Coarse RX seed selection moved lane 0 from tap 32 to 88 despite
neither image finding a clean seed window. Add a bounded alternate-seed
retry with a fresh DDR/BISC initialization and retain the full CK width and
center checks. Test normal and debug hardware separately; neither currently
qualifies for production because setup WNS is negative.


The first alternate-seed normal image improved 1:8 WNS to -0.283 ns but
failed CK training on all three attempts. Its first retry clobbered useful
TX seeds when a new sweep found no clean point and inherited the preceding
read phase; both firmware errors are corrected in the next build. The r6
debug image measured clean CK, RX, and TX windows on all four lanes, then
failed controller-based per-bit RX deskew with millions of bit errors.
Review found a concrete 1:8 phase-order mismatch: the throughput-preserving
DFI converter ordered commands as native phases 0..3 in each fast clock,
but permuted data across the two clocks. The converter and direct BIOS
calibration pattern now use the same phase order; DFI and firmware tests
pass. The corrected debug image routed at -0.895/+0.030/0.000 ns and passed
CK plus all four RX windows, but TX lane 3 had eight clean samples against
nine required, stopping at stage 4/error 8 before controller memory testing.
Apply the existing bounded RX-offset TX retry to 1:8 profiles, retaining
the full width and center checks, then verify controller data integrity.
The r7 normal build retains the old mapping and routes at -0.398 ns WNS,
so it is timing evidence only. The known-good component image was restored
after the failed corrected debug run and passed BIOS memory testing.

Advance one rate at a time on each FPGA part and speed grade. At each rate,
record exact bitstream/firmware hashes, WNS/WHS/WPWS, calibration result,
per-lane read/write windows, BIOS and 32 MiB CPU memory tests, repeated
reinitialization, separate fresh FPGA configuration, sequential DMA,
interleaved DMA, and error/fault counters. Add power-cycle and temperature/
voltage testing when hardware control is available. Test debug and normal
builds because optional logic can change placement.

### Two-slot integration update (2026-09-27)

The r36 AES-KU40 single-slot 2400 MT/s baseline now closes timing at
+0.070/+0.030/0.000 ns and passes three fresh configurations, eight full
calibrations and 16 DMA checks. Full 1 GiB sequential DMA is 4.252/4.304 GB/s.
The local workspace report `timing_work/AESKU40_R36_HARDWARE_RESULTS.md`
contains the exact evidence and retry limits.

Two-slot integration is opt-in and not yet hardware qualified. The selected
architecture keeps native addresses at one 256-bit BL8, with BG0 assigned to
the first four DFI phases and BG1 to the second four. Existing paired native
ports provide a 512-bit frontend. All-bank arbitration, global turnaround
admission, per-slot write capture, owner-tagged read returns and reserved
response queues are being connected to the production controller/crossbar.
Maintenance commands remain serialized. Existing bank recovery is rounded
conservatively for the later slot, and refresh waits for both groups.

Before building/programming: verify simultaneous CAS ownership, masks, odd
single-group accesses, row transitions, response backpressure and refresh
against an independent DFI memory scoreboard; verify actual rate-converter
phase/latency and complete native transmit mapping; adapt direct-DFII BIOS
calibration explicitly for eight full-width phases without changing r36's
reduced-width path. Then close final timing and repeat full-range hardware
tests. The new `--usnative-dual-slot` target flag is experimental until those
gates pass. No remote push is authorized for this work.

Integration now generates firmware/RTL and passes the full controller/crossbar
memory scoreboard with explicit and automatic precharge, queued paired reads,
partial masks, standalone accesses, refresh and physical-CK timing checks.
A separate Wishbone32-to-native256 test covers the CPU path. Full-rate converter
tests verify saturated responses and ownership across seven reset releases.
R38 uses zero optional TX data/mask stages and passes calibration and 32 MiB
CPU testing. Its paired DMA read fails with stale upper halves although CPU
readback confirms all DMA-written data. The read simulation had used offset
clock edges; correcting it to aligned rising edges shows the extra slot-1
register is wrong for this board. R39 disables that register. Require fresh
timing and hardware DMA integrity before accepting the corrected return path.
That r39 gate now passes: +0.096/+0.029/0.000 ns setup/hold/pulse slack,
three fresh loads, nine full calibrations, five CPU 32 MiB tests and 18 DMA
checks. Full 1 GiB PRBS31 achieves 8.313–8.388 GB/s write and 8.300 GB/s read,
with zero DMA errors/faults. One load needed successful CK-seed and TX-window
retries. The 18 DMA checks include two full-range checks after clearing
historical calibration diagnostics; PHY faults stay zero and all 12 controls
remain ready. Evidence
is in the local `timing_work/AESKU40_R39_HARDWARE_RESULTS.md` report. This is
one-board ambient testing, not power-cycle or thermal qualification.
The r36 reduced-width launch settings are not interchangeable with this mode.
Review also found and fixed refresh deadlock when ACT/PRE was pending: block
CAS immediately but allow maintenance to drain until all bank machines grant.
The r37e 2400 implementation includes these fixes and closes routed timing
at +0.012/+0.030/0.000 ns, but its first hardware load failed CK calibration
(stage 3/error 6). CPU/DMA testing was therefore not run. Initial coarse
scans with every sample failing had replaced boot seeds with arbitrary first
taps. The next firmware iteration retains seeds in that case and tests a
repeated static pattern across the full-width DFII slots. Its hardware
diagnostic reached a ten-sample CK window but failed controller writes: both
CPU and direct DFII reads retained old training data. R38's one-native-cycle
earlier payload/mask launch fixes this controller write failure and closes
timing at +0.032/+0.030/0.000 ns. Its DMA read failure was a separate
qualification blocker, resolved by r39's corrected return path. Keep these
build identities separate from r36's passes.

After this hardware gate, prioritize these follow-ups:

- Make the phase relationship explicit in converter regressions: aligned
  rising edges, independent reset release and unique identities in every
  slot. Include controller, converter and paired DMA in one regression.
  Remove the unused offset-phase read-register experiment after the normal
  return path is hardware qualified; do not retain a misleading board knob.
- Measure paired-CAS occupancy, refresh and row-transition stalls, response
  credits and write-data availability before tuning queue depth or arbitration.
- Replace conservative read-to-precharge/turnaround budgets only with measured
  or specified bounds and retain independent physical-CK assertions.
- Give the PHY/converter an explicit slot/latency contract so unsupported
  combinations fail during configuration. Qualify odd native read latency
  separately before extending two-slot support to every speed profile.
- Keep directed refresh-arrival tests for ACT, PRE and auto-precharge recovery;
  include long mixed CPU/DMA traffic and reset/reinitialization after saturation.
- Reduce optional trace/capture logic only after validating a normal build
  separately; timing and placement can change when diagnostics are removed.
- Preserve prior calibration seeds when a scan has no informative samples;
  distinguish this condition in logs from a measured, partly passing tap.
  Evaluate coupled CK/read-phase seed searches if retaining seeds is not
  enough, without relaxing the final window and memory-test requirements.
- If timing margin needs improvement, buffer per-master write payloads before
  CAS acceptance to break CAS-to-native-ready-to-CPU-ack feedback. Preserve
  the tested CAS-relative payload launch and avoid ready fall-through at full.

Do not claim timing closure from a hardware pass. If setup or hold is
negative, keep the image diagnostic. If native clock pulse-width slack is
negative, report the rate as an explicit overclock for that part and do not
convert it into a passing signoff with an XDC exception or artificial clock.
If the FPGA's documented clock or native primitive limit prevents 3200 on a
given part, carry the architecture to a supported UltraScale or UltraScale+
part and report that device separately.

### AES-KU40 2666.667 experiment, 2026-09-27

The r40b two-slot 1:8 / 512-bit DMA image was built at 166.667 MHz controller
and 333.333 MHz native PHY clocks. Host tests accept its CL19/CWL14 RD0/WR1
profile; the end-to-end paired-DMA behavioral regression also passes.
Two fresh volatile FPGA loads failed full CK-window calibration (stage 3,
error 6, no clean samples). No CPU memory or DMA performance result exists
for this image. See the local `timing_work/AESKU40_R40_2667_RESULTS.md` report.

Final routed slack is -0.054 ns setup, +0.021 ns hold, -0.330 ns pulse/minimum
period. The setup path is native pulse-synchronizer readback through the CSR
mux. Separately, the actual -1 part's bit-slice library limits and 1200 MHz
PLLE3 VCO ceiling are exceeded. Experimental bitstream generation required
an explicit build-local PDRC-182 severity downgrade; original reports remain.
This is not a timing-qualified rate and software changes cannot waive that.

Next steps if this overclock is pursued:

- Enable raw calibration debug to capture expected/returned DFI words,
  FIFO status, native readiness and probe matches at the failing CK scan.
  Check read arrival versus command phase before expanding tap searches.
- Register/snapshot diagnostic status in the CSR domain to shorten the
  native-to-CSR mux path; preserve pulse/event semantics and add a directed
  readback test. Do not apply a false path to a live status bus.
- Retain five-consecutive-sample and memory-test acceptance requirements;
  no clean samples does not justify lowering the calibration threshold.
- Keep FPGA clock-limit violations separate from calibration root-cause
  hypotheses. Move to a suitable device/speed grade for rated 2667 closure.

### Detailed improvement and DQ margin campaign

The local execution plan is
`hssio_experimental/timing_work/AESKU40_IMPROVEMENT_AND_DQ_WINDOWS_PLAN.md`
(relative to the outer workspace). It prioritizes coherent registered CSR
status, optional per-DQ debug sweeps, five 2400 FPGA reloads, then 2667 startup
failure isolation before higher-rate eye or performance claims. The current
32-DQ settings were captured without changing delays; actual independent
RX/TX window sweeps remain pending debug firmware support. A current tap
count is not a measured window width.
