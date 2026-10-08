# SPDX-License-Identifier: BSD-2-Clause
"""Check refreshed DDR4 command encoding through the AES-KU40 1:8 DFI path."""

import unittest

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.modules import EDY4016A
from litedram.phy.dfi import DDR4DFIMux, DFIRateConverter, Interface


class TestDualSlotRefreshPins(unittest.TestCase):
    def test_refresh_through_rate_converter_and_ddr4_command_mux(self):
        # AES-KU40 DDR4-2400 1:8 controller clock: 150 MHz, eight CK per
        # controller cycle. Keep the real module timing conversion here.
        frequency = 150e6
        module = EDY4016A(frequency, "1:8", speedgrade="2400")
        module.geom_settings.addressbits = 17
        self.assertEqual(module.timing_settings.tREFI, 1172)
        self.assertEqual(module.timing_settings.tRFC, 40)
        self.assertEqual(module.timing_settings.tRP, 3)
        # Keep the real 1:8 tRFC while shortening the refresh interval so this
        # controller-level simulation covers several refreshes quickly.
        simulated_trefi = 120
        module.timing_settings.tREFI = simulated_trefi

        phy = PhySettings(
            phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
            dfi_databits=64, nphases=8, nranks=1,
            rdphase=2, wrphase=3, cl=17, cwl=12, cmd_latency=5,
            read_latency=9, write_latency=1)
        controller = LiteDRAMController(
            phy, module.geom_settings, module.timing_settings, frequency,
            controller_settings=ControllerSettings(
                with_bank_group_interleaving=True,
                with_dual_slot=True,
                with_auto_precharge=False,
                with_refresh=True,
                refresh_postponing=1,
                with_registered_row_hit=True,
                with_registered_refresh_timers=True,
                with_registered_refresh_request=True,
                with_registered_timing_valid=True,
                with_activate_eligibility=True,
                read_time=32, write_time=16))

        native_dfi = Interface(17, 3, 1, 64, nphases=4)
        converter = DFIRateConverter(
            native_dfi, clkdiv="sys", clk="sys2x", ratio=2,
            write_delay=1, read_delay=0, preserve_throughput=True,
            serdes_reset=ResetSignal("sys"))
        pin_dfi = Interface(17, 3, 1, 64, nphases=4)
        ddr4_mux = DDR4DFIMux(native_dfi, pin_dfi)

        top = Module()
        top.clock_domains.cd_sys = ClockDomain("sys")
        top.clock_domains.cd_sys2x = ClockDomain("sys2x")
        top.submodules.controller = controller
        top.submodules.converter = converter
        top.submodules.ddr4_mux = ddr4_mux
        top.comb += controller.dfi.connect(converter.dfi)

        # The USNative PHY turns each DFI phase into two equal CA samples in
        # its eight-slot TX word. Check the phase fields that feed that mapping
        # and the native CA command-bit ordering without Vivado primitives.
        refresh_events = []
        command_errors = []
        samples_per_phase = 2
        horizon_fast = 2 * (3 * simulated_trefi + 64)

        def monitor():
            for fast_cycle in range(horizon_fast):
                phase_values = []
                for phase in pin_dfi.phases:
                    phase_values.append({
                        "cs_n": (yield phase.cs_n),
                        "act_n": (yield phase.act_n),
                        "address": (yield phase.address),
                        "bank": (yield phase.bank),
                        "ras_n": (yield phase.ras_n),
                        "cas_n": (yield phase.cas_n),
                        "we_n": (yield phase.we_n),
                    })

                refs = [index for index, value in enumerate(phase_values)
                        if value["cs_n"] == 0 and value["ras_n"] == 0
                        and value["cas_n"] == 0 and value["we_n"] == 1]
                if refs:
                    if len(refs) != 1:
                        command_errors.append((fast_cycle, "multiple REF phases", refs))
                    phase_index = refs[0]
                    value = phase_values[phase_index]
                    # DDR4 REF has ACT_n high and active-low RAS/CAS with
                    # WE_n high. DFI bit ordering in the native CA word is
                    # A[0:14], WE_n, CAS_n, RAS_n.
                    ca_sample = ((value["address"] & 0x3fff) |
                                 (value["we_n"] << 14) |
                                 (value["cas_n"] << 15) |
                                 (value["ras_n"] << 16))
                    command_code = (ca_sample >> 14) & 0x7
                    encoded = [command_code] * samples_per_phase
                    if value["act_n"] != 1:
                        command_errors.append((fast_cycle, "REF ACT_n not inactive", value))
                    if command_code != 0b001 or encoded != [0b001] * samples_per_phase:
                        command_errors.append((fast_cycle, "bad native RAS/CAS/WE ordering", hex(ca_sample)))
                    refresh_events.append((fast_cycle, phase_index, encoded))

                yield

        fragment = top.get_fragment()
        for special in fragment.specials:
            if isinstance(special, Memory):
                for port in special.ports:
                    if port.dat_r is None:
                        port.dat_r = Signal(special.width)
        run_simulation(fragment, {"sys2x": monitor()},
                       clocks={"sys": (8, 3), "sys2x": (4, 1)})

        self.assertEqual(command_errors, [])
        self.assertGreaterEqual(len(refresh_events), 2, refresh_events)
        self.assertTrue(all(event[1] == 0 for event in refresh_events), refresh_events)
        fast_period = 2 * simulated_trefi
        gaps = [b[0] - a[0] for a, b in zip(refresh_events, refresh_events[1:])]
        self.assertTrue(all(gap >= 2 * module.timing_settings.tRFC for gap in gaps), gaps)
        # Registered timer/request paths and the controller/DFI registration
        # may add a fixed phase offset, but must not change the steady period.
        self.assertTrue(all(abs(gap - fast_period) <= 2 for gap in gaps), gaps)


if __name__ == "__main__":
    unittest.main()
