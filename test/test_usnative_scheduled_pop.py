#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Simulation tests for the isolated scheduled FIFO-pop diagnostic helper."""

import unittest

from migen import Module, Mux, Signal
from migen.sim import run_simulation

from litedram.phy.usnative.scheduled_pop import ScheduledFIFOPop


class ScheduledFIFOPopHarness(Module):
    def __init__(self):
        self.submodules.dut = ScheduledFIFOPop(lanes=4)
        self.empty_idle = Signal(4)
        self.empty_during_rd_en = Signal(4)
        self.comb += self.dut.fifo_empty.eq(Mux(
            self.dut.rd_en, self.empty_during_rd_en, self.empty_idle))


class ScheduledFIFOPopTest(unittest.TestCase):
    def test_registered_pop_samples_delay_and_empty_only_for_diagnostics(self):
        harness = ScheduledFIFOPopHarness()
        dut = harness.dut
        samples = []

        def driver():
            # The request is independent of EMPTY.  Change EMPTY after the
            # RD_EN launch, using the harness mux, before the FIFO consumes
            # that registered pulse at the following edge.
            yield dut.delay.eq(0)
            yield harness.empty_idle.eq(0b0100)
            yield harness.empty_during_rd_en.eq(0b1001)
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            while not (yield dut.rd_en):
                yield
            for _ in range(4):
                yield

            # Delay is sampled per request: changing the input after this
            # request must not alter its 31-cycle wait.
            yield dut.delay.eq(31)
            yield harness.empty_idle.eq(0)
            yield harness.empty_during_rd_en.eq(0)
            yield
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            yield dut.delay.eq(0)
            for _ in range(40):
                yield

        def monitor():
            yield "passive"
            while True:
                yield
                samples.append(((yield dut.request_accepted), (yield dut.rd_en),
                                (yield dut.fifo_empty), (yield dut.missing_lanes),
                                (yield dut.underflow)))

        run_simulation(harness, [driver(), monitor()])
        accepted_indices = [i for i, (accepted, _, _, _, _) in enumerate(samples) if accepted]
        pop_indices = [i for i, (_, pop, _, _, _) in enumerate(samples) if pop]
        self.assertEqual(len(accepted_indices), 2)
        self.assertEqual(len(pop_indices), 2)
        self.assertEqual(pop_indices[0] - accepted_indices[0], 1)
        self.assertEqual(pop_indices[1] - accepted_indices[1], 32)
        # EMPTY at launch was 0100, then changed while RD_EN was high.  The
        # report appears one cycle later and reflects the actual pop edge.
        diag = samples[pop_indices[0] + 1]
        self.assertEqual(diag[3:], (0b1001, 1))
        self.assertEqual(samples[pop_indices[1] + 1][3:], (0, 0))

    def test_overlapping_request_is_reported_and_original_request_completes(self):
        dut = ScheduledFIFOPop(lanes=2)
        observed = []

        def driver():
            yield dut.delay.eq(3)
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            yield
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            for _ in range(5):
                yield

        def monitor():
            yield "passive"
            while True:
                yield
                observed.append(((yield dut.overlap), (yield dut.rd_en)))

        run_simulation(dut, [driver(), monitor()])
        self.assertEqual(sum(overlap for overlap, _ in observed), 1)
        self.assertEqual(sum(pop for _, pop in observed), 1)

    def test_ready_and_reset_cancel_pending_request(self):
        dut = ScheduledFIFOPop(lanes=1)

        def driver():
            yield dut.delay.eq(4)
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            yield dut.ready.eq(0)
            yield
            yield
            self.assertEqual((yield dut.busy), 0)
            self.assertEqual((yield dut.rd_en), 0)
            yield dut.ready.eq(1)
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            yield dut.reset.eq(1)
            yield
            yield
            self.assertEqual((yield dut.busy), 0)
            self.assertEqual((yield dut.rd_en), 0)
            yield dut.reset.eq(0)
            for _ in range(6):
                yield
                self.assertEqual((yield dut.rd_en), 0)

        run_simulation(dut, driver())

    def test_invalid_lane_count(self):
        for lanes in (0, -1, True, 1.5):
            with self.subTest(lanes=lanes):
                with self.assertRaises(ValueError):
                    ScheduledFIFOPop(lanes)


if __name__ == "__main__":
    unittest.main()
