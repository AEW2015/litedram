# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest

from migen import Array, If, Module, Signal
from migen.sim import run_simulation

from litedram.phy.usnative.ddrphy import (
    NativeFixedFIFOPopMonitor, NativeFixedReadTappedDelayLine, NativeRXBitslip,
    _native_fixed_fifo_pop_request,
)


class FixedPopHarness(Module):
    def __init__(self, read_latency=4, monitor_pipeline=False):
        self.request = Signal()
        self.ready = Signal(reset=1)
        self.vtc_ready = Signal(2, reset=3)
        self.reset = Signal()
        self.available = Signal(2, reset=3)
        self.clear = Signal()
        self.pop_events = Signal(32)
        self.valid_events = Signal(32)
        self.data0 = Signal(8)
        self.data1 = Signal(8)
        self.monitor_underflows = Signal(32)
        self.monitor_missing = Signal(2)

        rd = NativeFixedReadTappedDelayLine(self.request, ntaps=read_latency,
            flush=self.reset | ~self.ready | (self.vtc_ready != 3))
        self.submodules.rd = rd
        self.pop = _native_fixed_fifo_pop_request(
            rd.taps, read_latency, self.ready & (self.vtc_ready == 3), self.reset)
        self.valid = (rd.taps[read_latency - 1] & self.ready &
                      (self.vtc_ready == 3) & ~self.reset)
        monitor = NativeFixedFIFOPopMonitor(2, pipeline=monitor_pipeline)
        self.submodules.monitor = monitor
        self.comb += [
            monitor.pop.eq(self.pop),
            monitor.available.eq(self.available),
            monitor.clear.eq(self.clear),
            monitor.reset.eq(self.reset),
            self.monitor_underflows.eq(monitor.underflows),
            self.monitor_missing.eq(monitor.missing),
        ]
        words = [0x31, 0xa7]
        ptr0, ptr1 = Signal(1), Signal(1)
        q0, q1 = Array(words)[ptr0], Array(words)[ptr1]
        rx0 = NativeRXBitslip(q0, self.reset, Signal(1),
            held_return='pop_edge', accepted=self.pop & self.available[0])
        rx1 = NativeRXBitslip(q1, self.reset, Signal(1),
            held_return='pop_edge', accepted=self.pop & self.available[1])
        self.submodules += rx0, rx1
        self.comb += [self.data0.eq(rx0.o), self.data1.eq(rx1.o)]
        self.sync += [
            If(self.pop, self.pop_events.eq(self.pop_events + 1)),
            If(self.valid, self.valid_events.eq(self.valid_events + 1)),
            If(self.pop & self.available[0], ptr0.eq(ptr0 + 1)),
            If(self.pop & self.available[1], ptr1.eq(ptr1 + 1)),
        ]


class FixedPopMonitorHarness(Module):
    def __init__(self, pipeline=False):
        self.pop = Signal()
        self.available = Signal(2)
        self.clear = Signal()
        self.reset = Signal()
        self.underflows = Signal(32)
        self.missing = Signal(2)
        monitor = NativeFixedFIFOPopMonitor(2, pipeline=pipeline)
        self.submodules.monitor = monitor
        self.comb += [
            monitor.pop.eq(self.pop),
            monitor.available.eq(self.available),
            monitor.clear.eq(self.clear),
            monitor.reset.eq(self.reset),
            self.underflows.eq(monitor.underflows),
            self.missing.eq(monitor.missing),
        ]


class TestNativeFixedFIFOPop(unittest.TestCase):
    def test_latency_tap_pop_accepts_adjacent_reads_without_spacing_them(self):
        dut = FixedPopHarness(read_latency=4)
        observed = []

        def bench():
            # Two controller reads two cycles apart, the minimum supported
            # cadence for this test. Each response must capture its own head.
            for cycle in range(12):
                yield dut.request.eq(cycle in (0, 2))
                yield
                if (yield dut.valid):
                    observed.append(((yield dut.data0), (yield dut.data1)))
            self.assertEqual(observed, [(0x31, 0x31), (0xa7, 0xa7)])
            self.assertEqual((yield dut.pop_events), 2)
            self.assertEqual((yield dut.valid_events), 2)

        run_simulation(dut, bench())

    def test_empty_is_sampled_at_the_pop_and_records_missing_lanes(self):
        dut = FixedPopHarness(read_latency=3)
        underflows = []
        missing = []

        def bench():
            for cycle in range(8):
                yield dut.request.eq(cycle == 0)
                # EMPTY becomes late-visible: availability is asserted before
                # the scheduled pop edge, so this is a clean accepted pop.
                yield dut.available.eq(0 if cycle < 2 else 3)
                yield
                underflows.append((yield dut.monitor_underflows))
                missing.append((yield dut.monitor_missing))
            self.assertEqual(max(underflows), 0)
            self.assertEqual((yield dut.pop_events), 1)

        run_simulation(dut, bench())

    def test_any_missing_lane_is_counted_once_per_global_strobe(self):
        dut = FixedPopHarness(read_latency=3)
        snapshots = []

        def bench():
            for cycle in range(8):
                yield dut.request.eq(cycle == 0)
                yield dut.available.eq(1 if cycle < 2 else 2)
                yield
                snapshots.append(((yield dut.monitor_underflows),
                                  (yield dut.monitor_missing)))
            self.assertEqual(max(count for count, _ in snapshots), 1)
            self.assertEqual(snapshots[-1][1], 1)

        run_simulation(dut, bench())

    def test_reset_clear_and_not_ready_cancel_strobes(self):
        dut = FixedPopHarness(read_latency=3)
        observed = []
        valid = []

        def bench():
            for cycle in range(13):
                # Read 0 is canceled by ready dropping before its pop tap and
                # returning before its former valid tap. Read 1 is canceled
                # by reset; read 2 is a fresh post-reset request.
                yield dut.request.eq(cycle in (0, 4, 7))
                yield dut.ready.eq(cycle != 1)
                yield dut.reset.eq(cycle == 5)
                yield dut.clear.eq(cycle == 11)
                yield
                observed.append((yield dut.pop_events))
                valid.append((yield dut.valid_events))
            self.assertEqual(observed[6], 0)
            self.assertEqual(valid[6], 0)
            self.assertEqual(observed[-1], 1)
            self.assertEqual(valid[-1], 1)
            self.assertEqual((yield dut.monitor_underflows), 0)
            self.assertEqual((yield dut.monitor_missing), 0)

        run_simulation(dut, bench())

    def test_reset_masks_a_preexisting_valid_tap_and_allows_a_fresh_read(self):
        dut = FixedPopHarness(read_latency=3)
        visible_valid = []

        def bench():
            for cycle in range(11):
                yield dut.request.eq(cycle in (0, 5))
                yield dut.reset.eq(cycle == 3)
                yield
                if cycle == 2:
                    # The delayed valid tap has reached its old target; an
                    # asynchronous software reset must suppress this pulse.
                    yield dut.reset.eq(1)
                    self.assertEqual((yield dut.valid), 0)
                visible_valid.append((yield dut.valid))
            self.assertEqual(sum(visible_valid), 1)
            self.assertEqual((yield dut.pop_events), 2)  # first pop preceded reset
            self.assertEqual((yield dut.valid_events), 1)

        run_simulation(dut, bench())

    def test_vtc_loss_flushes_old_reads_even_if_ready_returns_first(self):
        dut = FixedPopHarness(read_latency=3)
        visible = []

        def bench():
            for cycle in range(11):
                yield dut.request.eq(cycle in (0, 5))
                yield dut.vtc_ready.eq(0 if cycle == 1 else 3)
                yield
                visible.append(((yield dut.pop_events), (yield dut.valid_events)))
            self.assertEqual(visible[5], (0, 0))
            self.assertEqual(visible[-1], (1, 1))

        run_simulation(dut, bench())

    def test_latency_and_monitor_geometry_are_checked(self):
        for latency, taps in ((1, [Signal()]), (4, [Signal()]*3)):
            with self.subTest(latency=latency, taps=len(taps)):
                with self.assertRaisesRegex(ValueError, 'read latency'):
                    _native_fixed_fifo_pop_request(taps, latency, Signal(), Signal())
        with self.assertRaisesRegex(ValueError, 'one to eight lanes'):
            NativeFixedFIFOPopMonitor(0)
        with self.assertRaisesRegex(ValueError, 'option must be boolean'):
            NativeFixedFIFOPopMonitor(2, pipeline=1)

    def test_pipelined_monitor_counts_back_to_back_pops_and_tracks_latest_bitmap(self):
        dut = FixedPopMonitorHarness(pipeline=True)
        snapshots = []

        def bench():
            for cycle in range(7):
                yield dut.pop.eq(cycle in (0, 1, 2))
                # Pop 0 is clean, pop 1 misses lane 1, pop 2 misses lane 0.
                yield dut.available.eq(3 if cycle == 0 else 1 if cycle == 1 else 2)
                yield
                snapshots.append(((yield dut.underflows), (yield dut.missing)))
            self.assertEqual(snapshots[:5], [(0, 0), (0, 0), (0, 0), (1, 2), (2, 1)])
            self.assertEqual(snapshots[-1], (2, 1))

        run_simulation(dut, bench())

    def test_pipeline_clear_and_reset_flush_pending_pop_and_bitmap(self):
        for signal_name in ('clear', 'reset'):
            with self.subTest(signal=signal_name):
                dut = FixedPopMonitorHarness(pipeline=True)
                snapshots = []

                def bench():
                    for cycle in range(6):
                        yield dut.pop.eq(cycle in (0, 3))
                        yield dut.available.eq(1)  # Missing lane 1 on both requests.
                        yield getattr(dut, signal_name).eq(cycle == 1)
                        yield
                        snapshots.append(((yield dut.underflows), (yield dut.missing)))
                    self.assertEqual(snapshots[1], (0, 0))
                    self.assertEqual(snapshots[2], (0, 0))
                    self.assertEqual(snapshots[-1], (1, 2))

                run_simulation(dut, bench())

    def test_pipeline_counter_remains_saturated_at_uint32_max(self):
        dut = FixedPopMonitorHarness(pipeline=True)
        dut.monitor.underflows.reset = 0xffffffff

        def bench():
            yield dut.pop.eq(1)
            yield dut.available.eq(1)
            yield
            yield dut.pop.eq(0)
            yield
            yield
            self.assertEqual((yield dut.underflows), 0xffffffff)
            self.assertEqual((yield dut.missing), 2)

        run_simulation(dut, bench())


if __name__ == '__main__':
    unittest.main()
