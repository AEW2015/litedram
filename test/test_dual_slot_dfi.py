# SPDX-License-Identifier: BSD-2-Clause
"""DFI mapping and rate-converter tests for dual-slot DDR4 BL8 transfers."""
import unittest

from migen import *
from litex.gen.sim import run_simulation

from litedram.core.dual_slot import DualSlotScheduler
from litedram.core.dual_slot_dfi import DualSlotDFI
from litedram.phy.dfi import Interface, DFIRateConverter


CLKS = {
    "sys":   (8, 3),
    "sys2x": (4, 1),
}


def pattern(seed):
    return int.from_bytes(bytes((seed + i) & 0xff for i in range(32)), "little")


def write_request(tag, group, bank, col, data, mask):
    return {
        "valid": 1, "group": group, "bank": bank, "col": col,
        "write": 1, "data": data, "mask": mask, "tag": tag,
    }


def read_request(tag, group, bank, col):
    return {
        "valid": 1, "group": group, "bank": bank, "col": col,
        "write": 0, "data": 0, "mask": 0, "tag": tag,
    }


def drive_request(dut, index, item):
    yield dut.req_valid[index].eq(item["valid"])
    yield dut.req_group[index].eq(item["group"])
    yield dut.req_bank[index].eq(item["bank"])
    yield dut.req_col[index].eq(item["col"])
    yield dut.req_write[index].eq(item["write"])
    yield dut.req_data[index].eq(item["data"])
    yield dut.req_mask[index].eq(item["mask"])
    yield dut.req_tag[index].eq(item["tag"])


class DirectDFIDUT(Module):
    def __init__(self, *, rdphase=1, wrphase=1):
        self.scheduler = DualSlotScheduler()
        self.submodules.scheduler = self.scheduler
        self.dfi = Interface(addressbits=17, bankbits=3, nranks=1,
                             databits=64, nphases=8)
        self.submodules.mapper = DualSlotDFI(
            self.scheduler, self.dfi, rdphase=rdphase, wrphase=wrphase)


class ConvertedDFIDUT(Module):
    def __init__(self, *, rdphase=1, wrphase=1):
        self.phy_dfi = Interface(addressbits=17, bankbits=3, nranks=1,
                                 databits=64, nphases=4)
        self.submodules.converter = DFIRateConverter(
            self.phy_dfi, clkdiv="sys", clk="sys2x", ratio=2,
            serdes_reset_cnt=-1, preserve_throughput=True)
        self.scheduler = DualSlotScheduler()
        self.submodules.scheduler = self.scheduler
        self.submodules.mapper = DualSlotDFI(
            self.scheduler, self.converter.dfi,
            rdphase=rdphase, wrphase=wrphase)


class TestDualSlotDFI(unittest.TestCase):
    def test_dual_write_maps_independent_payloads_masks_and_commands(self):
        dut = DirectDFIDUT(rdphase=1, wrphase=2)
        data0, data1 = pattern(0x10), pattern(0x80)
        mask0, mask1 = 0xA55AA55A, 0x3CC33CC3

        def main():
            yield from drive_request(dut.scheduler, 0,
                write_request(1, 0, 2, 0x155, data0, mask0))
            yield from drive_request(dut.scheduler, 1,
                write_request(2, 1, 1, 0x2AA, data1, mask1))
            yield
            for phase_index, phase in enumerate(dut.dfi.phases):
                slot = phase_index // 4
                offset = phase_index % 4
                cmd_phase = offset == 1  # wrphase=2 -> command phase 1.
                self.assertEqual((yield phase.cas_n), 0 if cmd_phase else 1)
                self.assertEqual((yield phase.cs_n), 0 if cmd_phase else 1)
                self.assertEqual((yield phase.we_n), 0 if cmd_phase else 1)
                self.assertEqual((yield phase.address),
                    (0x155, 0x2AA)[slot] if cmd_phase else 0)
                self.assertEqual((yield phase.bank),
                    ((2 | (0 << 2)), (1 | (1 << 2)))[slot] if cmd_phase else 0)
                self.assertEqual((yield phase.wrdata_en),
                    1 if offset == 2 else 0)
                expected_data = ((data0, data1)[slot] >> (64 * offset)) & ((1 << 64) - 1)
                expected_mask = ((mask0, mask1)[slot] >> (8 * offset)) & 0xff
                self.assertEqual((yield phase.wrdata), expected_data)
                self.assertEqual((yield phase.wrdata_mask), expected_mask)
                self.assertEqual((yield phase.rddata_en), 0)
            yield

        run_simulation(dut, main())

    def test_single_tail_has_no_second_slot_command_or_write_data(self):
        dut = DirectDFIDUT(rdphase=0, wrphase=1)
        data = pattern(0x31)

        def main():
            yield from drive_request(dut.scheduler, 0,
                write_request(3, 0, 0, 0x155, data, 0x12345678))
            yield dut.scheduler.req_valid[1].eq(0)
            yield
            for phase_index, phase in enumerate(dut.dfi.phases):
                if phase_index < 4:
                    continue
                self.assertEqual((yield phase.cs_n), 1)
                self.assertEqual((yield phase.cas_n), 1)
                self.assertEqual((yield phase.wrdata_en), 0)
                self.assertEqual((yield phase.wrdata), 0)
                self.assertEqual((yield phase.wrdata_mask), 0)
            yield

        run_simulation(dut, main())

    def test_two_reads_use_separate_command_enables_and_return_data(self):
        dut = DirectDFIDUT(rdphase=3, wrphase=1)
        data = [0x0102030405060708 * (i + 1) for i in range(8)]

        def main():
            yield from drive_request(dut.scheduler, 0, read_request(4, 0, 2, 0x111))
            yield from drive_request(dut.scheduler, 1, read_request(5, 1, 3, 0x222))
            for i, phase in enumerate(dut.dfi.phases):
                yield phase.rddata.eq(data[i])
                yield phase.rddata_valid.eq(1)
            yield
            for i, phase in enumerate(dut.dfi.phases):
                slot = i // 4
                offset = i % 4
                self.assertEqual((yield phase.cas_n), 0 if offset == 2 else 1)
                self.assertEqual((yield phase.we_n), 1)
                self.assertEqual((yield phase.rddata_en), 1 if offset == 3 else 0)
                self.assertEqual((yield phase.wrdata_en), 0)
                self.assertEqual((yield phase.wrdata), 0)
                self.assertEqual((yield phase.address),
                    (0x111, 0x222)[slot] if offset == 2 else 0)
            expected0 = sum(data[i] << (64*i) for i in range(4))
            expected1 = sum(data[i+4] << (64*i) for i in range(4))
            self.assertEqual((yield dut.mapper.slot_rdata[0]), expected0)
            self.assertEqual((yield dut.mapper.slot_rdata[1]), expected1)
            self.assertEqual((yield dut.mapper.slot_read_valid[0]), 1)
            self.assertEqual((yield dut.mapper.slot_read_valid[1]), 1)
            yield dut.dfi.p6.rddata_valid.eq(0)
            yield
            self.assertEqual((yield dut.mapper.slot_read_valid[0]), 1)
            self.assertEqual((yield dut.mapper.slot_read_valid[1]), 0)
            yield

        run_simulation(dut, main())

    def test_full_throughput_converter_preserves_two_write_slots_and_four_ck_spacing(self):
        dut = ConvertedDFIDUT(rdphase=0, wrphase=2)
        data0, data1 = pattern(0x20), pattern(0xA0)
        mask0, mask1 = 0x01234567, 0x89ABCDEF
        observed = []

        def drive():
            yield from drive_request(dut.scheduler, 0,
                write_request(10, 0, 1, 0x101, data0, mask0))
            yield from drive_request(dut.scheduler, 1,
                write_request(11, 1, 2, 0x202, data1, mask1))
            for _ in range(40):
                yield

        def check():
            for _ in range(10):
                yield
            for fast_cycle in range(16):
                commands = []
                for phase_index, phase in enumerate(dut.phy_dfi.phases):
                    if (yield phase.cas_n) == 0:
                        self.assertEqual((yield phase.ras_n), 1)
                        commands.append((phase_index, (yield phase.address),
                                         (yield phase.bank), (yield phase.we_n)))
                self.assertEqual(len(commands), 1)
                phase_index, address, bank, we_n = commands[0]
                group = bank >> 2
                expected_address = 0x101 if group == 0 else 0x202
                expected_data = data0 if group == 0 else data1
                expected_mask = mask0 if group == 0 else mask1
                self.assertEqual(phase_index, 1)  # wrphase=2 => command offset 1.
                self.assertEqual(address, expected_address)
                self.assertEqual(we_n, 0)
                for pindex, phase in enumerate(dut.phy_dfi.phases):
                    expected_word = (expected_data >> (64*pindex)) & ((1 << 64) - 1)
                    expected_byte_mask = (expected_mask >> (8*pindex)) & 0xff
                    self.assertEqual((yield phase.wrdata), expected_word)
                    self.assertEqual((yield phase.wrdata_mask), expected_byte_mask)
                    self.assertEqual((yield phase.wrdata_en), 1 if pindex == 2 else 0)
                observed.append((fast_cycle * 4 + phase_index, group))
                yield

        run_simulation(dut, {"sys": [drive()], "sys2x": [check()]}, clocks=CLKS)
        self.assertEqual(len(observed), 16)
        ck = [event[0] for event in observed]
        self.assertEqual([b-a for a, b in zip(ck, ck[1:])], [4] * 15)
        groups = [group for _, group in observed]
        self.assertEqual(groups, [groups[0] ^ (i & 1) for i in range(16)])

    def test_full_throughput_converter_returns_both_read_burst_slots(self):
        dut = ConvertedDFIDUT(rdphase=1, wrphase=2)
        captured = []

        def drive_requests():
            yield from drive_request(dut.scheduler, 0, read_request(20, 0, 1, 0x111))
            yield from drive_request(dut.scheduler, 1, read_request(21, 1, 2, 0x222))
            for _ in range(40):
                yield

        def drive_phy_reads():
            for fast_cycle in range(64):
                frame = 0x1111111111111100 if fast_cycle & 1 == 0 else 0x2222222222222200
                for phase, p in enumerate(dut.phy_dfi.phases):
                    yield p.rddata.eq(frame + phase)
                    yield p.rddata_valid.eq(1)
                yield

        def check_controller_reads():
            for _ in range(12):
                yield
            for _ in range(16):
                valid0 = (yield dut.mapper.slot_read_valid[0])
                valid1 = (yield dut.mapper.slot_read_valid[1])
                if valid0 and valid1:
                    captured.append(((yield dut.mapper.slot_rdata[0]),
                                     (yield dut.mapper.slot_rdata[1])))
                yield

        run_simulation(dut, {
            "sys": [drive_requests(), check_controller_reads()],
            "sys2x": [drive_phy_reads()],
        }, clocks=CLKS)
        self.assertGreaterEqual(len(captured), 4)
        possible = []
        for parity in (0, 1):
            frame = 0x1111111111111100 if parity == 0 else 0x2222222222222200
            possible.append(sum((frame + phase) << (64*phase) for phase in range(4)))
        for slot0, slot1 in captured:
            self.assertIn(slot0, possible)
            self.assertIn(slot1, possible)
            self.assertNotEqual(slot0, slot1)


if __name__ == "__main__":
    unittest.main()
