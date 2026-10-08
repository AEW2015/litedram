#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Exercise the 32-bit Wishbone CPU path through a dual-slot DDR4 controller."""

import unittest
from collections import defaultdict

from migen import *
from litex.gen.sim import run_simulation
from litex.soc.interconnect import wishbone

from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.frontend.wishbone import LiteDRAMWishbone2Native
from litedram.modules import EDY4016A


class DualSlotCPUPathTest(unittest.TestCase):
    def test_wishbone32_masked_writes_and_reads_across_bank_groups(self):
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
            with_auto_precharge=True, with_refresh=False,
            with_registered_row_hit=True,
            with_registered_timing_valid=True,
            with_activate_eligibility=True,
            read_time=16, write_time=8)
        controller = LiteDRAMController(
            phy, module.geom_settings, module.timing_settings, frequency,
            controller_settings=settings)
        top = Module()
        top.submodules.controller = controller
        crossbar = LiteDRAMCrossbar(controller.interface)
        top.submodules.crossbar = crossbar
        native = crossbar.get_port()
        wb = wishbone.Interface(adr_width=24, data_width=32)
        top.submodules.wishbone_frontend = LiteDRAMWishbone2Native(wb, native)
        dfi = controller.dfi

        state = {
            "cycle": 0, "open_rows": {}, "memory": {}, "read_latches": [0, 0],
            "reads_due": {}, "writes_due": defaultdict(list), "commands": [],
        }

        @passive
        def dfi_memory():
            while True:
                cycle = state["cycle"]
                due = state["reads_due"].pop(cycle, {})
                for slot in range(2):
                    if slot in due:
                        state["read_latches"][slot] = due[slot]
                # Drive each slot's 256-bit return over its four 64-bit phases.
                for slot in range(2):
                    value = state["read_latches"][slot]
                    for offset, phase in enumerate(dfi.phases[slot*4:slot*4+4]):
                        yield phase.rddata.eq((value >> (64*offset)) & ((1 << 64) - 1))
                        yield phase.rddata_valid.eq(int(slot in due))

                cas = []
                for index, phase in enumerate(dfi.phases):
                    cs_n, ras_n = (yield phase.cs_n), (yield phase.ras_n)
                    cas_n, we_n = (yield phase.cas_n), (yield phase.we_n)
                    bank, address = (yield phase.bank), (yield phase.address)
                    if not cs_n and not ras_n and cas_n and we_n:
                        state["open_rows"][bank] = address
                        state["commands"].append((cycle, "ACT", index, bank, address))
                    elif not cs_n and ras_n and not cas_n:
                        slot = index // 4
                        kind = "WR" if not we_n else "RD"
                        row = state["open_rows"].get(bank, 0)
                        key = (bank, row, address & 0x3ff)
                        cas.append((slot, kind, key))
                        state["commands"].append((cycle, kind, index, bank, address))
                        if address & (1 << 10):
                            state["open_rows"].pop(bank, None)

                for slot, kind, key in cas:
                    if kind == "RD":
                        state["reads_due"].setdefault(cycle + phy.read_latency - 1, {})[slot] = state["memory"].get(key, 0)
                    else:
                        state["writes_due"][(cycle + phy.write_latency, slot)].append(key)
                for slot in range(2):
                    for key in state["writes_due"].pop((cycle, slot), []):
                        value = mask = 0
                        for offset, phase in enumerate(dfi.phases[slot*4:slot*4+4]):
                            value |= (yield phase.wrdata) << (64*offset)
                            mask |= (yield phase.wrdata_mask) << (8*offset)
                        old = state["memory"].get(key, 0)
                        for byte in range(32):
                            if not ((mask >> byte) & 1):
                                byte_mask = 0xff << (8*byte)
                                old = (old & ~byte_mask) | (value & byte_mask)
                        state["memory"][key] = old
                state["cycle"] += 1
                yield

        expected = [0] * 16
        write_values = [None] * 16
        # Two eight-beat bursts map to consecutive native 256-bit words,
        # hence alternate the interleaved bank group (BG0 then BG1).
        for block in range(2):
            for lane in range(8):
                value = (0x10203040 + block*8 + lane*0x101) & 0xffffffff
                sel = 0x5 if lane % 2 == 0 else 0xa
                byte_mask = sum(0xff << (8*byte) for byte in range(4)
                                if (sel >> byte) & 1)
                expected[block*8 + lane] = value & byte_mask
                write_values[block*8 + lane] = (value, sel)

        observed = []

        def testbench():
            for block in range(2):
                yield wb.cyc.eq(1)
                yield wb.stb.eq(1)
                yield wb.we.eq(1)
                for lane in range(8):
                    i = block*8 + lane
                    value, sel = write_values[i]
                    yield wb.adr.eq(i)
                    yield wb.sel.eq(sel)
                    yield wb.dat_w.eq(value)
                    yield wb.cti.eq(wishbone.CTI_BURST_END if lane == 7
                                    else wishbone.CTI_BURST_INCREMENTING)
                    yield
                    for _ in range(500):
                        if (yield wb.ack):
                            break
                        yield
                    else:
                        self.fail("Wishbone write timed out at address {}".format(i))
                yield wb.cyc.eq(0)
                yield wb.stb.eq(0)
                yield wb.cti.eq(wishbone.CTI_BURST_NONE)
                yield
            for _ in range(40):
                yield
            for block in range(2):
                yield wb.cyc.eq(1)
                yield wb.stb.eq(1)
                yield wb.we.eq(0)
                for lane in range(8):
                    i = block*8 + lane
                    yield wb.adr.eq(i)
                    yield wb.sel.eq(0xf)
                    yield wb.cti.eq(wishbone.CTI_BURST_END if lane == 7
                                    else wishbone.CTI_BURST_INCREMENTING)
                    yield
                    for _ in range(500):
                        if (yield wb.ack):
                            observed.append((yield wb.dat_r))
                            break
                        yield
                    else:
                        self.fail("Wishbone read timed out at address {}".format(i))
                yield wb.cyc.eq(0)
                yield wb.stb.eq(0)
                yield wb.cti.eq(wishbone.CTI_BURST_NONE)
                yield

        fragment = top.get_fragment()
        for special in fragment.specials:
            if isinstance(special, Memory):
                for port in special.ports:
                    if port.dat_r is None:
                        port.dat_r = Signal(special.width)
        run_simulation(fragment, [testbench(), dfi_memory()])

        expected_values = expected
        self.assertEqual(observed, expected_values,
                         "commands={!r} memory={!r}".format(state["commands"], state["memory"]))
        written_groups = {cmd[3] for cmd in state["commands"] if cmd[1] == "WR"}
        read_groups = {cmd[3] for cmd in state["commands"] if cmd[1] == "RD"}
        self.assertTrue({0, 4}.issubset(written_groups), state["commands"])
        self.assertTrue({0, 4}.issubset(read_groups), state["commands"])


if __name__ == "__main__":
    unittest.main()
