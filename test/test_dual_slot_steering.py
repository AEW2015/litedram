#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Exercise the real two-slot multiplexer and registered DFI steerer."""

from types import SimpleNamespace
import unittest

from migen import *
from litex.gen.sim import run_simulation
from litex.soc.interconnect import stream

from litedram.common import LiteDRAMInterface, cmd_request_layout, cmd_request_rw_layout
from litedram.core.dual_slot_multiplexer import DualSlotMultiplexer
from litedram.phy.dfi import Interface as DFIInterface


class DualSlotSteeringTest(unittest.TestCase):
    def make_dut(self, *, bankbits=3, phase_width=64):
        nbanks = 1 << bankbits
        settings = SimpleNamespace(
            with_dual_slot=True,
            with_bandwidth=False,
            with_registered_refresh_request=False,
            read_time=0,
            write_time=0,
            phy=SimpleNamespace(
                nphases=8, rdphase=Signal(2), wrphase=Signal(2),
                nranks=1, dfi_databits=phase_width, cl=17, cwl=12, read_latency=6),
            geom=SimpleNamespace(addressbits=13, bankbits=bankbits, colbits=10,
                                 rowbits=13),
            timing=SimpleNamespace(tWTR=2, tCCD=1, tRRD=1, tFAW=16))
        interface = LiteDRAMInterface(address_align=3, settings=settings)
        dfi = DFIInterface(addressbits=13, bankbits=bankbits, nranks=1,
                           databits=phase_width, nphases=8)
        bank_layout = cmd_request_rw_layout(13, bankbits)
        banks = []
        for _ in range(nbanks):
            banks.append(SimpleNamespace(
                cmd=stream.Endpoint(bank_layout),
                activate_ready=Signal(reset=1),
                refresh_req=Signal(),
                refresh_gnt=Signal(reset=1)))
        refresh_layout = cmd_request_rw_layout(13, bankbits)
        refresher = SimpleNamespace(cmd=stream.Endpoint(refresh_layout))

        top = Module()
        top.submodules.multiplexer = mux = DualSlotMultiplexer(
            settings, banks, refresher, dfi, interface)
        return top, mux, banks, refresher, dfi, interface, settings

    def exercise_pair(self, phase, write, maintenance=False, *, bankbits=3,
                      phase_width=64):
        top, mux, banks, refresher, dfi, interface, settings = self.make_dut(
            bankbits=bankbits, phase_width=phase_width)
        accepted = []
        issued = []
        enables = []
        maintenance_count = []

        def main():
            yield settings.phy.rdphase.eq(phase)
            yield settings.phy.wrphase.eq(phase)
            yield interface.bank_read_ready.eq((1 << len(banks)) - 1)
            yield interface.bank_write_ready.eq((1 << len(banks)) - 1)

            # Slot 0 is bank 0 (group 0); slot 1 is bank 4 (group 1).
            for bank in (0, 4):
                cmd = banks[bank].cmd
                yield cmd.a.eq(0x120 + bank)
                yield cmd.ba.eq(bank)
                yield cmd.ras.eq(0)
                yield cmd.cas.eq(1)
                yield cmd.we.eq(int(write))
                yield cmd.is_cmd.eq(0)
                yield cmd.is_read.eq(int(not write))
                yield cmd.is_write.eq(int(write))
                yield cmd.valid.eq(1)

            if maintenance:
                # An ACT owns phase zero while CAS data-enable phase is zero.
                cmd = banks[2].cmd
                yield cmd.a.eq(0x55)
                yield cmd.ba.eq(2)
                yield cmd.ras.eq(1)
                yield cmd.cas.eq(0)
                yield cmd.we.eq(0)
                yield cmd.is_cmd.eq(1)
                yield cmd.is_read.eq(0)
                yield cmd.is_write.eq(0)
                yield cmd.valid.eq(1)

            for cycle in range(20):
                # Record true accepted events, then remove each fake source at
                # the next edge just as a BankMachine retires its endpoint.
                if maintenance and (yield mux.arbiter.cmd.valid) and (yield mux.arbiter.cmd.ready):
                    maintenance_count.append(cycle)
                    yield banks[2].cmd.valid.eq(0)
                for slot, bank in enumerate((0, 4)):
                    cmd = mux.arbiter.cas_cmd[slot]
                    if (yield cmd.valid) and (yield cmd.ready):
                        accepted.append((cycle, slot))
                        yield banks[bank].cmd.valid.eq(0)

                # The actual _Steerer registers the selected command/data
                # enables onto DFI. Count what the PHY sees, not its inputs.
                for index, output in enumerate(dfi.phases):
                    if (yield output.cas_n) == 0 and (yield output.ras_n) == 1:
                        issued.append((cycle, index, (yield output.we_n)))
                    if (yield output.rddata_en) or (yield output.wrdata_en):
                        enables.append((index, (yield output.rddata_en),
                                        (yield output.wrdata_en)))
                    if index == 0 and (yield output.ras_n) == 0 and (yield output.cas_n) == 1:
                        maintenance_count.append(("dfi", cycle))
                yield
                if len(accepted) == 2 and len([x for x in issued if x[0] in range(8)]) >= 2:
                    # Keep observing a few cycles to detect duplicate pulses.
                    if cycle > 8:
                        break

            self.assertEqual(len(accepted), 2)
            command_phase = phase
            self.assertEqual([(index, we_n) for _, index, we_n in issued], [
                (command_phase, 0 if write else 1),
                (command_phase + 4, 0 if write else 1),
            ])
            expected_enable = (phase, 0 if write else 1, 1 if write else 0)
            expected_second = (phase + 4, 0 if write else 1, 1 if write else 0)
            self.assertEqual(enables, [expected_enable, expected_second])
            if maintenance:
                self.assertEqual(len([x for x in maintenance_count if isinstance(x, tuple)]), 1)
                act_cycles = [event[1] for event in maintenance_count
                              if isinstance(event, tuple) and event[0] == "dfi"]
                cas0_cycles = [cycle for cycle, index, _ in issued if index == 0]
                self.assertTrue(all(cas_cycle != act_cycle
                                    for act_cycle in act_cycles
                                    for cas_cycle in cas0_cycles))
                self.assertEqual(len([x for x in maintenance_count
                                      if isinstance(x, int)]), 1)

        run_simulation(top, main())

    def test_all_programmable_phase_offsets_route_both_slots(self):
        for phase in range(4):
            for write in (False, True):
                with self.subTest(phase=phase, write=write):
                    self.exercise_pair(phase, write)

    def test_phase_zero_maintenance_is_exclusive_then_both_cas_retire(self):
        self.exercise_pair(phase=0, write=False, maintenance=True)

    def test_x64_four_group_pair_uses_128_bit_dfi_phases(self):
        self.exercise_pair(phase=2, write=False, bankbits=4, phase_width=128)


if __name__ == "__main__":
    unittest.main()
