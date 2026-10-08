#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Cycle-accurate lane alignment for native RX FIFO read enables."""

import random
import unittest
from functools import reduce
from operator import or_

from migen import Module, Signal
from migen.sim import run_simulation

from litedram.phy.usnative.ddrphy import _lane_aligned_fifo_available


class LaneAvailabilityDUT(Module):
    def __init__(self, lane_count):
        self.empty = Signal(8*lane_count)
        self.lane_available = [Signal(name=f"lane{lane}_available")
            for lane in range(lane_count)]
        for lane, available in enumerate(self.lane_available):
            empty_bits = [self.empty[8*lane + bit] for bit in range(8)]
            self.comb += available.eq(~reduce(or_, empty_bits))
        self.aligned_available = _lane_aligned_fifo_available(self,
            self.lane_available)


class TestUSNativeFIFOControl(unittest.TestCase):
    def test_byte_local_terms_preserve_common_drain_semantics(self):
        for lane_count in (1, 2, 4, 8):
            with self.subTest(lane_count=lane_count):
                dut = LaneAvailabilityDUT(lane_count)

                def process():
                    rng = random.Random(0x8320 + lane_count)
                    for _ in range(1000):
                        empty = rng.getrandbits(8*lane_count)
                        yield dut.empty.eq(empty)
                        yield
                        lane_ready = []
                        for lane in range(lane_count):
                            lane_mask = (empty >> (8*lane)) & 0xff
                            lane_ready.append(lane_mask == 0)
                            self.assertEqual((yield dut.lane_available[lane]),
                                int(lane_mask == 0))
                        expected = int(all(lane_ready))
                        for lane in range(lane_count):
                            self.assertEqual((yield dut.aligned_available[lane]),
                                expected, (lane_count, empty, lane))

                run_simulation(dut, process())


if __name__ == "__main__":
    unittest.main()
