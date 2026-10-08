#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Simulation of native DQS FIFO write-clock activity counters."""

import unittest

from migen import Module, Signal
from migen.sim import run_simulation

from litedram.phy.usnative.clock_monitor import NativeDQSClockMonitor, gray_to_binary


class NativeDQSClockMonitorTest(unittest.TestCase):
    def test_idle_dqs_clocks_leave_counters_unchanged(self):
        clocks = [Signal(name=f'dqs{lane}') for lane in range(2)]
        dut = NativeDQSClockMonitor(clocks, width=8)

        def bench():
            for _ in range(12):
                yield
            self.assertEqual((yield dut.counts[0]), 0)
            self.assertEqual((yield dut.counts[1]), 0)

        run_simulation(dut, bench(), clocks={'sys': 10})

    def test_asynchronous_burst_clock_edges_are_synchronized(self):
        clocks = [Signal(name=f'dqs{lane}') for lane in range(2)]
        dut = NativeDQSClockMonitor(clocks, width=8, connect_clocks=False)

        def bench():
            # Non-harmonic source clocks exercise Gray sampling at different
            # phases relative to sys while each source domain free-runs.
            for _ in range(20):
                yield
            first = []
            for count in dut.counts:
                first.append((yield count))
            for _ in range(30):
                yield
            second = []
            for count in dut.counts:
                second.append((yield count))
            for before, after in zip(first, second):
                self.assertGreater(after, before)

        run_simulation(dut, bench(), clocks={
            'sys': 10, 'usnative_dqs_wrclk0': 14, 'usnative_dqs_wrclk1': 18})

    def test_gray_decode(self):
        # Exercise values with multi-bit binary transitions; adjacent Gray
        # values still differ by exactly one bit before synchronization.
        for binary in range(256):
            gray = binary ^ (binary >> 1)
            self.assertEqual(gray_to_binary(gray, 8), binary)


if __name__ == '__main__':
    unittest.main()
