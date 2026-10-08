#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest

from migen import *

from litedram.common import LiteDRAMNativePort
from litedram.core.dual_slot_data import DualSlotData
from test.test_native_benchmark import simulate


class DualSlotDataTest(unittest.TestCase):
    def make_dut(self, *, depth=4, read_latency=2, write_latency=2, data_width=256):
        ports = [LiteDRAMNativePort("both", 10, data_width) for _ in range(2)]
        dut = DualSlotData(ports, depth=depth, read_latency=read_latency,
                           write_latency=write_latency)
        return dut, ports

    def test_distinct_owner_masked_writes_and_stall(self):
        dut, ports = self.make_dut(write_latency=2)
        values = [0x123456789abcdef, (1 << 255) | 0x55aa]
        masks = [0x01234567, 0x89abcdef]

        def main():
            for i, port in enumerate(ports):
                yield port.wdata.valid.eq(1)
                yield port.wdata.data.eq(values[i])
                yield port.wdata.we.eq(masks[i])
            for slot in range(2):
                yield dut.slot_valid[slot].eq(1)
                yield dut.slot_write[slot].eq(1)
                yield dut.slot_owner[slot].eq(slot)
            yield
            self.assertEqual((yield dut.slot_ready[0]), 1)
            self.assertEqual((yield dut.slot_ready[1]), 1)
            for port in ports:
                self.assertEqual((yield port.wdata.ready), 1)
            yield
            # Latency two means the captured payload reaches the slot after
            # two registered stages; the first visible cycle is after two ticks.
            self.assertEqual((yield dut.slot_wdata_valid[0]), 0)
            yield
            self.assertEqual((yield dut.slot_wdata_valid[0]), 1)
            self.assertEqual((yield dut.slot_wdata[0]), values[0])
            self.assertEqual((yield dut.slot_wdata_we[0]), masks[0])
            self.assertEqual((yield dut.slot_wdata[1]), values[1])
            self.assertEqual((yield dut.slot_wdata_we[1]), masks[1])
            # Remove one lane's data head. That lane stalls independently.
            yield dut.slot_valid[0].eq(0)
            yield ports[1].wdata.valid.eq(0)
            yield
            self.assertEqual((yield dut.slot_ready[0]), 1)
            self.assertEqual((yield dut.slot_ready[1]), 0)
            # A pair attributed to one native owner is prohibited.
            yield ports[1].wdata.valid.eq(1)
            yield dut.slot_valid[0].eq(1)
            yield dut.slot_owner[1].eq(0)
            yield
            yield
            self.assertEqual((yield dut.slot_ready[0]), 0)
            self.assertEqual((yield dut.slot_ready[1]), 0)
            self.assertEqual((yield dut.error), 1)

        simulate(dut, main())

    def test_x64_512_bit_slots_preserve_full_payload_and_mask(self):
        dut, ports = self.make_dut(depth=1, data_width=512,
                                   read_latency=1, write_latency=1)
        payload = (1 << 511) | (1 << 256) | 0x987654321
        mask = (1 << 63) | (1 << 32) | 0x55
        readback = (1 << 510) | (1 << 255) | 0x123456789
        readback1 = (1 << 509) | (1 << 254) | 0x987654321

        def main():
            yield ports[0].wdata.valid.eq(1)
            yield ports[0].wdata.data.eq(payload)
            yield ports[0].wdata.we.eq(mask)
            yield dut.slot_valid[0].eq(1)
            yield dut.slot_write[0].eq(1)
            yield dut.slot_owner[0].eq(0)
            yield
            self.assertEqual((yield ports[0].wdata.ready), 1)
            yield
            self.assertEqual((yield dut.slot_wdata[0]), payload)
            self.assertEqual((yield dut.slot_wdata_we[0]), mask)
            self.assertEqual(len(dut.slot_wdata[0]), 512)
            self.assertEqual(len(dut.slot_wdata_we[0]), 64)

            yield dut.slot_valid[0].eq(0)
            yield dut.slot_write[0].eq(0)
            yield dut.slot_rdata[0].eq(readback)
            yield dut.slot_rdata[1].eq(readback1)
            for port in ports:
                yield port.rdata.ready.eq(0)
            yield dut.slot_valid[0].eq(1)
            yield dut.slot_valid[1].eq(1)
            yield dut.slot_write[1].eq(0)
            yield dut.slot_owner[1].eq(1)
            yield
            yield dut.slot_valid[0].eq(0)
            yield dut.slot_valid[1].eq(0)
            yield
            yield
            self.assertEqual((yield ports[0].rdata.valid), 1)
            self.assertEqual((yield ports[0].rdata.data), readback)
            self.assertEqual((yield ports[1].rdata.valid), 1)
            self.assertEqual((yield ports[1].rdata.data), readback1)
            self.assertEqual((yield dut.master_read_ready[0]), 0)
            self.assertEqual((yield dut.master_read_ready[1]), 0)
            for port in ports:
                yield port.rdata.ready.eq(1)
            yield
            self.assertEqual((yield dut.error), 0)

        simulate(dut, main())

    def test_read_credit_stalls_until_fixed_return_is_consumed(self):
        dut, ports = self.make_dut(depth=1, read_latency=2)

        def main():
            for port in ports:
                yield port.rdata.ready.eq(0)
            yield dut.slot_valid[0].eq(1)
            yield dut.slot_write[0].eq(0)
            yield dut.slot_owner[0].eq(0)
            self.assertEqual((yield dut.master_read_ready[0]), 1)
            yield
            yield dut.slot_valid[0].eq(0)
            yield
            self.assertEqual((yield dut.master_read_ready[0]), 0)
            # One more read cannot be accepted while the first response is
            # outstanding, even though no data has returned yet.
            self.assertEqual((yield dut.master_read_ready[0]), 0)
            # Return at the configured fixed latency. It is buffered without
            # a ready from the physical side and retains owner 0.
            yield dut.slot_rdata[0].eq(0xfeedface)
            yield
            yield
            self.assertEqual((yield ports[0].rdata.valid), 1)
            self.assertEqual((yield ports[0].rdata.data), 0xfeedface)
            self.assertEqual((yield dut.master_read_ready[0]), 0)
            yield ports[0].rdata.ready.eq(1)
            yield
            yield ports[0].rdata.ready.eq(0)
            yield
            self.assertEqual((yield dut.master_read_ready[0]), 1)
            self.assertEqual((yield dut.error), 0)

        simulate(dut, main())

    def test_reserved_credit_depth_prevents_return_overflow(self):
        dut, ports = self.make_dut(depth=2, read_latency=2)

        def main():
            yield ports[0].rdata.ready.eq(0)
            # Two sequential commands from one master reserve both slots.
            for _ in range(2):
                yield dut.slot_valid[0].eq(1)
                yield dut.slot_write[0].eq(0)
                yield dut.slot_owner[0].eq(0)
                self.assertEqual((yield dut.master_read_ready[0]), 1)
                yield
            yield dut.slot_valid[0].eq(0)
            yield
            self.assertEqual((yield dut.master_read_ready[0]), 0)
            # The two fixed returns fit in the reserved response capacity even
            # while the native requester is stalled.
            yield dut.slot_rdata[0].eq(0x11)
            yield
            yield dut.slot_rdata[0].eq(0x22)
            yield
            yield
            self.assertEqual((yield dut.error), 0)
            self.assertEqual((yield dut.master_read_ready[0]), 0)
            self.assertEqual((yield ports[0].rdata.valid), 1)

        simulate(dut, main())

    def test_consecutive_returns_keep_independent_owners(self):
        dut, ports = self.make_dut(read_latency=2)
        observed = [[], []]

        def main():
            for port in ports:
                yield port.rdata.ready.eq(1)
            # Accept distinct owners on adjacent cycles.
            yield dut.slot_valid[0].eq(1)
            yield dut.slot_write[0].eq(0)
            yield dut.slot_owner[0].eq(0)
            yield
            yield dut.slot_owner[0].eq(1)
            yield
            yield dut.slot_valid[0].eq(0)
            # Data halves are presented on the two fixed due cycles.
            yield dut.slot_rdata[0].eq(0xa0)
            yield
            yield dut.slot_rdata[0].eq(0xb1)
            yield
            for _ in range(3):
                for i, port in enumerate(ports):
                    if (yield port.rdata.valid):
                        observed[i].append((yield port.rdata.data))
                yield
            self.assertEqual(observed, [[0xa0], [0xb1]])
            self.assertEqual((yield dut.error), 0)

        simulate(dut, main())


if __name__ == "__main__":
    unittest.main()
