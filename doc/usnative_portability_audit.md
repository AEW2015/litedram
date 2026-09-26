# USNativeDDRPHY portability audit

This audit describes the local experimental branches before wider-board changes.
It is a source audit, not evidence that a board has a calibrated native PHY.

| Layer | Existing behavior | Required for wider DDR3/DDR4 boards |
| --- | --- | --- |
| Vivado package query | Validates package pin, bank, native site, control site, byte and DQS relationship; cache is versioned and optional. | Keep this query. Distinguish unsupported bank types and unsupported clock routing with explicit diagnostics. |
| Logical layout | Accepts x8 groups and up to eight lanes, but assumes each logical DQ octet shares its indexed DQS. | Derive/validate DQ-to-DQS grouping from the queried byte group. Support legal permutations, reject incompatible wiring. |
| Integrated PHY | Requires exactly x16, two DQS and DM, one bank, a14/ba2/bg1, one rank, DDR4 and four phases. | Derive data/mask/strobe width, command/address width and bank ownership. Resolve multiple PLL banks before x64 integration. |
| Data path | Hard-codes 16 DQ, 2 byte lanes, 32 DFI bits per phase, 16 DQS pattern bits and a two-lane FIFO barrier. | Size all signals, selectors, status and serialization from layout; preserve the four-phase DFI relationship. |
| Clock/profile | Four fixed DDR4 rates and one PLL clock; targets supply separate CRGs. | Preserve explicit rates, derive board clocks and reject unsupported MMCM/PLL combinations. Multiple banks require legal native clock distribution. |
| Firmware ABI | Versioned logical tap mapping exists but `mapping.h` requires 16 DQ/2 lanes and allocates 21 taps. | Size/validate arrays from generated counts; keep gateware/BIOS configuration identity checks. |
| Firmware training | Direct DFII read/write probes, bit deskew, per-lane windows and optional DMA calibration assume two lanes and 32-bit DFI words. | Generalize commands and data masks to x32/x64, dynamic lane loops and software-visible selectors. Test each lane. |
| DDR3 | No native DDR3 profile, initialization or calibration path; existing targets use component mode. | Add separate DDR3 timing/commands/training and test before offering native DDR3. A DDR4 profile cannot safely be selected for DDR3. |
| Board integration | XEM8320 has a special native CRG, clock profile, BIOS flags and optional DMA. Other targets select component PHY. | Add opt-in target integration using each board's real pins, memory module and clock/reset. Keep component PHY as default. |
| Evidence | XEM8320 images and hardware tests exist; no wider board bitstreams on these local branches. | Separate pin extraction, elaboration, synthesis, place, route, bitstream and hardware results. Report WNS/WHS/WPWS without timing waivers. |

The existing lower-level core generator is capable of describing multiple lanes
and banks. That does not imply the integrated DFI PHY or BIOS calibration is
width-independent. In particular, dropping the x16 check alone would silently
produce invalid training and must not be presented as x32/x64 support.

## Progress after the audit

The integrated RTL now sizes its DFI data bus, DQS patterns, lane selectors,
FIFO drains and optional trace from requested pads, and accepts a clock/lock/
enable vector with one entry per queried bank. The clock provider still has to
instantiate a local PLL for every bank. The BIOS training profile remains x16
DDR4; no wider image should be advertised as calibrated. DDR3 support at this
stage is limited to board pin extraction.

AMD's [UltraScale SelectIO guide](https://docs.amd.com/api/khub/documents/kFbaUC5HGcXyGNauhgU6Gw/content)
states that native BITSLICE_CONTROL PLL_CLK uses dedicated clock routing from
a PLL adjacent to the I/O bank. Its multiple-bank bring-up guidance requires
coordinated reset and readiness. A shared fabric clock wired to every bank's
PLL_CLK would be an invalid shortcut; board integration needs per-bank PLLs.
