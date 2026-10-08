# SPDX-License-Identifier: BSD-2-Clause
"""Check same-owner bank-group locking with and without buffered lookahead."""

import unittest

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.modules import EDY4016A


class DualSlotOwnershipTest(unittest.TestCase):
    def run_case(self, buffered):
        frequency = 150e6
        module = EDY4016A(frequency, "1:8", speedgrade="2400")
        module.timing_settings.tCCD = 1
        phy = PhySettings(
            phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
            dfi_databits=64, nphases=8, rdphase=2, wrphase=3, cl=17,
            cwl=12, cmd_latency=5, read_latency=9, write_latency=1, nranks=1)
        phy.rdphase = Signal(2, reset=2)
        phy.wrphase = Signal(2, reset=3)
        settings = ControllerSettings(
            with_bank_group_interleaving=True, with_dual_slot=True,
            with_auto_precharge=False, with_refresh=False,
            cmd_buffer_buffered=buffered,
            with_registered_row_hit=True,
            with_registered_timing_valid=True,
            with_activate_eligibility=True,
            read_time=16, write_time=8)
        controller = LiteDRAMController(
            phy, module.geom_settings, module.timing_settings, frequency,
            controller_settings=settings)
        bank_machines = [m for _, m in controller._submodules
                         if hasattr(m, "activate_ready")]
        top = Module()
        top.submodules.controller = controller
        crossbar = LiteDRAMCrossbar(controller.interface)
        top.submodules.crossbar = crossbar
        port = crossbar.get_port(mode="write")
        dfi = controller.dfi

        addresses = [0, 1, 0, 1, 0, 1, 0, 1]
        values = [(1 << (i * 17)) | (0x12345678 + i) for i in range(len(addresses))]
        state = {
            "cycle": 0, "accepted_cmd": [], "accepted_data": [],
            "cas": [], "launches": [], "violations": [],
        }

        @passive
        def monitor_dfi():
            while True:
                cycle = state["cycle"]
                cycle_cas = []
                for index, phase in enumerate(dfi.phases):
                    cs_n, ras_n = (yield phase.cs_n), (yield phase.ras_n)
                    cas_n, we_n = (yield phase.cas_n), (yield phase.we_n)
                    bank = (yield phase.bank)
                    if not cs_n and ras_n and not cas_n:
                        kind = "WR" if not we_n else "RD"
                        cycle_cas.append((index // 4, kind, bank))
                    if (yield phase.wrdata_en):
                        state["launches"].append((cycle, index))
                if len(cycle_cas) > 1:
                    state["violations"].append((cycle, "same-owner dual CAS", cycle_cas))
                for slot, kind, bank in cycle_cas:
                    state["cas"].append((cycle, slot, kind, bank))
                state["cycle"] += 1
                yield

        def drive_commands():
            yield port.cmd.we.eq(1)
            yield port.cmd.last.eq(1)
            yield port.cmd.addr.eq(addresses[0])
            yield port.cmd.valid.eq(1)
            for i, address in enumerate(addresses):
                for _ in range(400):
                    yield
                    if (yield port.cmd.ready):
                        state["accepted_cmd"].append((state["cycle"], address))
                        break
                else:
                    self.fail("write command timed out at index {}".format(i))
                if i + 1 < len(addresses):
                    yield port.cmd.addr.eq(addresses[i + 1])
            yield port.cmd.valid.eq(0)
            for _ in range(600):
                if len(state["accepted_data"]) == len(values):
                    break
                yield
            else:
                banks = []
                for index, bm in enumerate(bank_machines):
                    if (yield bm.cmd.valid):
                        banks.append((index, (yield bm.cmd.is_read), (yield bm.cmd.is_write),
                                      (yield bm.cmd.is_cmd), (yield bm.cmd.ba),
                                      (yield bm.cmd.a)))
                self.fail("write data timed out: {} of {} accepted; commands={!r}; "
                          "banks={!r}; fsm={}; CAS={!r}".format(
                    len(state["accepted_data"]), len(values), state["accepted_cmd"],
                    banks, (yield controller.multiplexer.fsm.state), state["cas"]))
            yield port.wdata.valid.eq(0)
            yield port.cmd.valid.eq(0)
            for _ in range(80):
                yield
            self.assertEqual((yield crossbar.dual_slot_data.error), 0,
                             "buffered={} CAS={!r}".format(buffered, state["cas"]))

        def drive_write_data():
            index = 0
            yield port.wdata.valid.eq(1)
            yield port.wdata.we.eq((1 << 32) - 1)
            yield port.wdata.data.eq(values[0])
            yield
            while index < len(values):
                if (yield port.wdata.ready):
                    state["accepted_data"].append((state["cycle"], index))
                    index += 1
                    if index < len(values):
                        yield port.wdata.data.eq(values[index])
                yield
            yield port.wdata.valid.eq(0)

        fragment = top.get_fragment()
        for special in fragment.specials:
            if isinstance(special, Memory):
                for mem_port in special.ports:
                    if mem_port.dat_r is None:
                        mem_port.dat_r = Signal(special.width)
        run_simulation(fragment, [drive_commands(), drive_write_data(), monitor_dfi()])

        self.assertEqual(state["violations"], [], state["violations"])
        self.assertEqual(len(state["accepted_cmd"]), len(addresses))
        self.assertEqual([address for _, address in state["accepted_cmd"]], addresses)
        self.assertEqual(len(state["accepted_data"]), len(values))
        self.assertEqual(len(state["cas"]), len(values), state["cas"])
        self.assertEqual(len(state["launches"]), len(values), state["launches"])
        self.assertEqual({slot for _, slot, _, _ in state["cas"]}, {0, 1})
        self.assertTrue(all(kind == "WR" for _, _, kind, _ in state["cas"]))

    def test_unbuffered_command_lookahead(self):
        self.run_case(buffered=False)

    def test_buffered_command_lookahead(self):
        self.run_case(buffered=True)


if __name__ == "__main__":
    unittest.main()
