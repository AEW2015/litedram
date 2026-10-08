#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest

from migen import *

from litedram.phy.dfi import Interface, DFIRateConverter
from test.phy_common import run_simulation


class FullRateDUT(Module):
    def __init__(self, databits, *, write_delay=0, read_delay=0, preserve_throughput=True,
                 repeat_write_data=False, early_write_data=False):
        self.phy_dfi = Interface(addressbits=17, bankbits=3, nranks=1,
            databits=databits, nphases=4)
        self.submodules.converter = DFIRateConverter(self.phy_dfi,
            clkdiv="sys", clk="sys2x", ratio=2, serdes_reset_cnt=-1,
            write_delay=write_delay, read_delay=read_delay,
            preserve_throughput=preserve_throughput,
            repeat_write_data=repeat_write_data,
            early_write_data=early_write_data)
        self.controller_dfi = self.converter.dfi


class TestDFIFullRate(unittest.TestCase):
    clocks = {
        "sys":   (8, 3),
        "sys2x": (4, 1),
    }

    def test_aggregate_width(self):
        for phy_width, controller_width in ((64, 64), (32, 32)):
            with self.subTest(phy_width=phy_width):
                dut = FullRateDUT(phy_width)
                self.assertEqual(len(dut.controller_dfi.phases), 8)
                self.assertEqual(len(dut.controller_dfi.p0.wrdata), controller_width)
                self.assertEqual(sum(len(p.wrdata) for p in dut.controller_dfi.phases),
                    2 * 4 * phy_width)

    def test_one_bl8_transaction_uses_reduced_width_and_one_fast_slot(self):
        # A x64 PHY at 2x clock has capacity for two x32 BL8 transactions
        # per controller cycle. The DDR4 controller issues one BL8, so its
        # eight DFI phases must carry 8*32 bits and occupy one fast slot.
        dut = FullRateDUT(64, preserve_throughput=False)
        controller = dut.controller_dfi
        phy = dut.phy_dfi
        self.assertEqual(len(controller.phases), 8)
        self.assertEqual([len(p.wrdata) for p in controller.phases], [32] * 8)
        self.assertEqual(sum(len(p.wrdata) for p in controller.phases), 8 * 32)

        data = [0x10203040 + 0x1010101 * phase for phase in range(8)]
        masks = [((phase * 3) + 1) & 0xf for phase in range(8)]

        def drive_controller():
            for _ in range(20):
                for phase, p in enumerate(controller.phases):
                    yield p.wrdata.eq(data[phase])
                    yield p.wrdata_mask.eq(masks[phase])
                yield

        def check_phy():
            for _ in range(8):
                yield
            for fast_cycle in range(16):
                # The default reduced-width layout packs each adjacent pair
                # into one PHY phase during slot zero. Slot one carries no
                # second transaction.
                slot = (fast_cycle + 1) & 1
                for phase, p in enumerate(phy.phases):
                    expected_data = (data[2*phase] | (data[2*phase + 1] << 32)) if slot == 0 else 0
                    expected_mask = (masks[2*phase] | (masks[2*phase + 1] << 4)) if slot == 0 else 0
                    self.assertEqual((yield p.wrdata), expected_data)
                    self.assertEqual((yield p.wrdata_mask), expected_mask)
                yield

        run_simulation(dut, {"sys": [drive_controller()], "sys2x": [check_phy()]},
            clocks=self.clocks)

    def test_reduced_width_write_delay_selects_second_slot_but_commands_use_first(self):
        dut = FullRateDUT(64, write_delay=1, preserve_throughput=False)
        controller = dut.controller_dfi
        phy = dut.phy_dfi
        data = [0x10203040 + 0x1010101 * phase for phase in range(8)]
        masks = [((phase * 3) + 1) & 0xf for phase in range(8)]

        def drive_controller():
            for _ in range(20):
                for phase, p in enumerate(controller.phases):
                    yield p.address.eq(0x100 + phase)
                    yield p.bank.eq(phase & 7)
                    yield p.cs_n.eq(0)
                    yield p.cas_n.eq(0 if phase in (2, 3) else 1)
                    yield p.ras_n.eq(1)
                    yield p.we_n.eq(0 if phase == 3 else 1)
                    yield p.wrdata_en.eq(1 if phase == 3 else 0)
                    yield p.rddata_en.eq(1 if phase == 2 else 0)
                    yield p.wrdata.eq(data[phase])
                    yield p.wrdata_mask.eq(masks[phase])
                yield

        def check_phy():
            for _ in range(8):
                yield
            for fast_cycle in range(16):
                command_slot = (fast_cycle + 1) & 1
                data_slot = command_slot
                for phase, p in enumerate(phy.phases):
                    source = phase + 4*command_slot
                    self.assertEqual((yield p.address), 0x100 + source)
                    self.assertEqual((yield p.rddata_en), 1 if source == 2 else 0)
                    self.assertEqual((yield p.wrdata_en), 1 if source == 3 else 0)
                    self.assertEqual((yield p.we_n), 0 if source == 3 else 1)
                    # write_delay=1 selects the second fast slot for data;
                    # controller phases 2*p and 2*p+1 still pack into each
                    # PHY phase, independently of the command phase mapping.
                    expected_data = (data[2*phase] | (data[2*phase + 1] << 32)) if data_slot == 1 else 0
                    expected_mask = (masks[2*phase] | (masks[2*phase + 1] << 4)) if data_slot == 1 else 0
                    self.assertEqual((yield p.wrdata), expected_data)
                    self.assertEqual((yield p.wrdata_mask), expected_mask)
                yield

        run_simulation(dut, {"sys": [drive_controller()], "sys2x": [check_phy()]},
            clocks=self.clocks)

    def test_reduced_width_repeats_payload_without_repeating_write_enable(self):
        dut = FullRateDUT(64, write_delay=1, preserve_throughput=False,
            repeat_write_data=True)
        controller = dut.controller_dfi
        phy = dut.phy_dfi
        data = [0x10203040 + 0x1010101 * phase for phase in range(8)]
        masks = [(phase * 3 + 1) & 0xf for phase in range(8)]

        def drive_controller():
            for _ in range(20):
                for phase, p in enumerate(controller.phases):
                    yield p.wrdata.eq(data[phase])
                    yield p.wrdata_mask.eq(masks[phase])
                    yield p.wrdata_en.eq(1 if phase == 3 else 0)
                yield

        def check_phy():
            for _ in range(8):
                yield
            for fast_cycle in range(16):
                slot = (fast_cycle + 1) & 1
                for phase, p in enumerate(phy.phases):
                    expected_data = data[2*phase] | (data[2*phase + 1] << 32)
                    expected_mask = masks[2*phase] | (masks[2*phase + 1] << 4)
                    self.assertEqual((yield p.wrdata), expected_data)
                    self.assertEqual((yield p.wrdata_mask), expected_mask)
                    self.assertEqual((yield p.wrdata_en), 1 if phase + 4*slot == 3 else 0)
                yield

        run_simulation(dut, {"sys": [drive_controller()], "sys2x": [check_phy()]},
            clocks=self.clocks)

    def test_early_write_data_removes_one_controller_cycle(self):
        def first_data_cycle(early):
            dut = FullRateDUT(64, preserve_throughput=False,
                repeat_write_data=True, early_write_data=early)
            observed = []

            def drive_controller():
                for cycle in range(12):
                    yield dut.controller_dfi.p0.wrdata.eq(0x12345678 if cycle >= 4 else 0)
                    yield

            def check_phy():
                for _ in range(24):
                    observed.append((yield dut.phy_dfi.p0.wrdata) & 0xffffffff)
                    yield

            run_simulation(dut, {"sys": [drive_controller()], "sys2x": [check_phy()]},
                clocks=self.clocks)
            return observed.index(0x12345678)

        self.assertEqual(first_data_cycle(False) - first_data_cycle(True), 2)

    def test_reduced_width_readback_unpacks_adjacent_phase_pairs(self):
        dut = FullRateDUT(64, write_delay=1, read_delay=0, preserve_throughput=False)
        controller = dut.controller_dfi
        phy = dut.phy_dfi
        data = [0x8877665544332200 + phase * 0x0101010101010101 for phase in range(4)]

        def drive_phy():
            for _ in range(40):
                for phase, p in enumerate(phy.phases):
                    yield p.rddata.eq(data[phase])
                    yield p.rddata_valid.eq(1)
                yield

        def check_controller():
            for _ in range(12):
                yield
            for _ in range(8):
                for phase, p in enumerate(controller.phases):
                    phy_word = data[phase // 2]
                    expected = (phy_word >> (32 * (phase & 1))) & 0xffffffff
                    self.assertEqual((yield p.rddata), expected)
                    self.assertEqual((yield p.rddata_valid), 1)
                yield

        run_simulation(dut, {"sys": [check_controller()], "sys2x": [drive_phy()]},
            clocks=self.clocks)

    def test_commands_keep_phase_order_across_both_fast_cycles(self):
        dut = FullRateDUT(64)
        controller = dut.controller_dfi
        phy = dut.phy_dfi
        values = [0x101 + phase for phase in range(8)]

        def drive_controller():
            for _ in range(20):
                for phase, p in enumerate(controller.phases):
                    yield p.address.eq(values[phase])
                    yield p.bank.eq(phase & 7)
                    yield p.cs_n.eq(0)
                    yield p.cke.eq(1)
                    yield p.odt.eq(phase & 1)
                    yield p.reset_n.eq(phase & 1)
                    yield p.act_n.eq(phase & 1)
                    yield p.cas_n.eq((phase >> 0) & 1)
                    yield p.ras_n.eq((phase >> 1) & 1)
                    yield p.we_n.eq((phase >> 2) & 1)
                yield

        def check_phy():
            # Allow the serializer's one controller-cycle input register and
            # reset phase to settle before checking repeated full-rate slots.
            for _ in range(8):
                yield
            for fast_cycle in range(16):
                slot = (fast_cycle + 1) & 1
                for phase, p in enumerate(phy.phases):
                    source = phase + 4*slot
                    self.assertEqual((yield p.address), values[source])
                    self.assertEqual((yield p.bank), source & 7)
                    self.assertEqual((yield p.cs_n), 0)
                    self.assertEqual((yield p.cke), 1)
                    self.assertEqual((yield p.odt), source & 1)
                    self.assertEqual((yield p.reset_n), source & 1)
                    self.assertEqual((yield p.act_n), source & 1)
                    self.assertEqual((yield p.cas_n), (source >> 0) & 1)
                    self.assertEqual((yield p.ras_n), (source >> 1) & 1)
                    self.assertEqual((yield p.we_n), (source >> 2) & 1)
                yield

        run_simulation(dut, {"sys": [drive_controller()], "sys2x": [check_phy()]},
            clocks=self.clocks)

    def test_consecutive_write_data_uses_both_fast_cycles(self):
        dut = FullRateDUT(64, write_delay=1)
        controller = dut.controller_dfi
        phy = dut.phy_dfi
        data = [0x1020304050607000 + phase for phase in range(8)]
        masks = [phase ^ 0x5a for phase in range(8)]

        def drive_controller():
            for _ in range(20):
                for phase, p in enumerate(controller.phases):
                    yield p.wrdata.eq(data[phase])
                    yield p.wrdata_mask.eq(masks[phase])
                    yield p.wrdata_en.eq(1)
                yield

        def check_phy():
            for _ in range(8):
                yield
            for fast_cycle in range(16):
                # The fast-cycle delay rotates the two controller slots;
                # neither cycle is dropped or replaced with zero.
                slot = fast_cycle & 1
                for phase, p in enumerate(phy.phases):
                    source = phase + 4*slot
                    self.assertEqual((yield p.wrdata), data[source])
                    self.assertEqual((yield p.wrdata_mask), masks[source])
                    self.assertEqual((yield p.wrdata_en), 1)
                yield

        run_simulation(dut, {"sys": [drive_controller()], "sys2x": [check_phy()]},
            clocks=self.clocks)

    def test_read_data_and_valid_cover_both_fast_cycles(self):
        dut = FullRateDUT(64, read_delay=1)
        controller = dut.controller_dfi
        phy = dut.phy_dfi
        data = [0x8877665544332200 + phase for phase in range(4)]

        def drive_phy():
            for _ in range(40):
                for phase, p in enumerate(phy.phases):
                    yield p.rddata.eq(data[phase])
                    yield p.rddata_valid.eq(1)
                yield

        def check_controller():
            for _ in range(8):
                yield
            for _ in range(8):
                for phase, p in enumerate(controller.phases):
                    self.assertEqual((yield p.rddata), data[phase % 4])
                    self.assertEqual((yield p.rddata_valid), 1)
                yield

        run_simulation(dut, {"sys": [check_controller()], "sys2x": [drive_phy()]},
            clocks=self.clocks)


if __name__ == "__main__":
    unittest.main()
