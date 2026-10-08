#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Measure dual-slot WRITE-to-PRE recovery on emitted DFI CK edges."""

import unittest
from math import ceil

from migen import *
from litex.gen.sim import run_simulation

from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.modules import EDY4016A
from litedram.common import PhySettings


class TestDualSlotWritePrecharge(unittest.TestCase):
    def _run_case(self, bank):
        # Match the AES-KU40 x32 DDR4 1:8 scheduler profile at 2400 MT/s.
        frequency = 150e6
        module = EDY4016A(frequency, "1:8", speedgrade="2400")
        module.timing_settings.tCCD = 1
        phy = PhySettings(
            phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
            dfi_databits=64, nphases=8, rdphase=2, wrphase=3, cl=17,
            cwl=12, cmd_latency=5, read_latency=12, write_latency=1, nranks=1)
        phy.rdphase = Signal(2, reset=2)
        phy.wrphase = Signal(2, reset=3)
        settings = ControllerSettings(
            with_bank_group_interleaving=True, with_dual_slot=True,
            with_auto_precharge=False, with_refresh=False,
            with_registered_timing_valid=True,
            with_registered_row_hit=True, with_activate_eligibility=True,
            read_time=32, write_time=16)
        controller = LiteDRAMController(
            phy, module.geom_settings, module.timing_settings, frequency,
            controller_settings=settings)
        top = Module()
        top.submodules.controller = controller
        crossbar = LiteDRAMCrossbar(controller.interface)
        top.submodules.crossbar = crossbar
        port = crossbar.get_port(mode="write")
        dfi = controller.dfi

        # Address mapping puts bank bits at address[7:10] for BL8. Pick a
        # row change in one bank so a real explicit PRE follows the WRITE.
        # Interleaved mapping is bank={addr[9:8], addr[0]}: the BG bit
        # is native word address bit zero.
        first_address = (bank >> 2) | ((bank & 0x3) << 8)
        next_row_address = first_address | (1 << 10)
        events = {"cycle": 0, "writes": [], "pres": []}

        def bench():
            def observe():
                for phase_index, phase in enumerate(dfi.phases):
                    cs_n = (yield phase.cs_n)
                    ras_n = (yield phase.ras_n)
                    cas_n = (yield phase.cas_n)
                    we_n = (yield phase.we_n)
                    if not cs_n and ras_n and not cas_n and not we_n:
                        events["writes"].append((events["cycle"] * 8 + phase_index,
                                                  phase_index, (yield phase.bank)))
                    if not cs_n and not ras_n and cas_n and not we_n:
                        events["pres"].append((events["cycle"] * 8 + phase_index,
                                               phase_index, (yield phase.bank)))

            def tick():
                yield from observe()
                yield
                events["cycle"] += 1

            def issue_write(address, data):
                yield port.cmd.addr.eq(address)
                yield port.cmd.we.eq(1)
                yield port.cmd.valid.eq(1)
                for _ in range(400):
                    yield from tick()
                    if (yield port.cmd.ready):
                        break
                else:
                    self.fail("write command timed out")
                yield port.cmd.valid.eq(0)
                yield port.wdata.data.eq(data)
                yield port.wdata.we.eq((1 << 32) - 1)
                yield port.wdata.valid.eq(1)
                for _ in range(400):
                    yield from tick()
                    if (yield port.wdata.ready):
                        break
                else:
                    self.fail("write data timed out")
                yield port.wdata.valid.eq(0)

            yield from issue_write(first_address, 0x12345678)
            yield from issue_write(next_row_address, 0x87654321)
            # Continue sampling until the second row is activated; its ACT
            # proves the preceding same-bank PRE has left the scheduler.
            for _ in range(100):
                yield from tick()
                if len(events["writes"]) >= 2 and events["pres"]:
                    break

        fragment = top.get_fragment()
        for special in fragment.specials:
            if isinstance(special, Memory):
                for memory_port in special.ports:
                    if memory_port.dat_r is None:
                        memory_port.dat_r = Signal(special.width)
        run_simulation(fragment, bench())

        self.assertEqual(len(events["writes"]), 2, events)
        # Slot 0/group 0 is phase 3; slot 1/group 1 is phase 7.
        expected_phase = 3 if bank == 0 else 7
        self.assertEqual([phase for _ck, phase, _ba in events["writes"]],
                         [expected_phase, expected_phase], events)
        self.assertTrue(events["pres"], events)
        pre_ck, pre_phase, pre_bank = events["pres"][0]
        self.assertEqual(pre_phase, 0, events)
        self.assertEqual(pre_bank, bank, events)
        first_write_ck = events["writes"][0][0]
        # Count half-CK edges: the eight DDR beats are spaced by half a CK,
        # so the final beat is 7 half-CK after the first CWL-delayed beat.
        last_write_ui_half_ck = 2 * first_write_ck + 2 * phy.cwl + 7
        # MR0 WR=28 at 2400 is more conservative than EDY4016A tWR=15 ns
        # (18 CK). Check against the encoded WR28 requirement from the final
        # BL8 write-data edge, not a source-level BankMachine countdown.
        self.assertGreaterEqual(2 * pre_ck - last_write_ui_half_ck, 2 * 28,
            "PRE={} CK, last BL8 UI={} half-CK, writes={}".format(
                pre_ck, last_write_ui_half_ck, events["writes"]))
        # CAS-to-final-data is CWL + 7/2 CK. WR28 therefore requires at
        # least 43.5 CK from the observed CAS edge (compare in half-CKs).
        self.assertGreaterEqual(2 * (pre_ck - first_write_ck),
                                2 * phy.cwl + 7 + 2 * 28, events)
        return pre_ck - first_write_ck

    def test_slot0_and_slot1_wr28_on_actual_dfi_edges(self):
        slot0_cas_to_pre = self._run_case(bank=0)
        slot1_cas_to_pre = self._run_case(bank=4)
        slot1_other_bank_cas_to_pre = self._run_case(bank=7)
        self.assertGreaterEqual(slot0_cas_to_pre, slot1_cas_to_pre - 8)
        self.assertGreaterEqual(slot1_other_bank_cas_to_pre,
                                slot1_cas_to_pre - 8)


if __name__ == "__main__":
    unittest.main()
