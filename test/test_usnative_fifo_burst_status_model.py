# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Bounded FIFO_EMPTY/read-enable model for burst-gated native RX FIFOs.

This is not an RXTX_BITSLICE simulation.  It models the documented two-read-
clock EMPTY status latency around an eight-entry FIFO and is intended to test
whether a drain policy is safe at burst boundaries.  The vendor primitive's
exact pointer and Q timing still requires Vivado simulation or hardware.
"""

from collections import deque
import unittest


class BurstFIFOModel:
    def __init__(self, lanes, *, status_latency=2):
        self.queues = [deque() for _ in range(lanes)]
        self.empty_pipes = [[True] * status_latency for _ in range(lanes)]
        self.registered_common = False
        self.underflows = [0] * lanes
        self.pops = [[] for _ in range(lanes)]
        self.pop_cycles = [[] for _ in range(lanes)]
        self.cycle = 0

    def tick(self, writes=(), *, policy="common"):
        """Advance one FIFO_RD_CLK edge.

        ``writes`` contains ``(lane, token)`` entries arriving on this edge.
        EMPTY reflects actual occupancy after the configured synchronization
        delay.  A pop from an empty model queue records a stale-Q over-read.
        """
        lane_available = [not pipe[0] for pipe in self.empty_pipes]
        common_data = all(lane_available)
        if policy == "common":
            drains = [common_data] * len(self.queues)
        elif policy == "registered-common":
            drains = [self.registered_common] * len(self.queues)
        elif policy == "local":
            drains = lane_available
        else:
            raise ValueError(policy)

        next_registered_common = common_data
        for lane, token in writes:
            self.queues[lane].append(token)
        for lane, drain in enumerate(drains):
            if drain:
                if self.queues[lane]:
                    self.pops[lane].append(self.queues[lane].popleft())
                    self.pop_cycles[lane].append(self.cycle)
                else:
                    self.underflows[lane] += 1
        for lane, queue in enumerate(self.queues):
            self.empty_pipes[lane] = self.empty_pipes[lane][1:] + [not queue]
        self.registered_common = next_registered_common
        self.cycle += 1


class FIFOReadReturnModel:
    """Track accepted native reads separately from their delayed Q returns.

    UG861 documents a typical FIFO return latency of two read clocks, with
    designs that can take as many as eight.  FIFO_EMPTY's documented two
    clock status delay is a separate path; treating FIFO_RD_EN as if it made
    Q valid in the same cycle hides the missing adapter contract.
    """
    def __init__(self, *, response_latency):
        if not 1 <= response_latency <= 8:
            raise ValueError("response latency must be in the documented 1..8 model range")
        self.response_latency = response_latency
        self.accepted = []
        self.returns = []
        self.pipeline = [None] * response_latency
        self.cycle = 0

    def tick(self, read_enable, token=None):
        returned = self.pipeline.pop(0)
        self.pipeline.append(token if read_enable else None)
        if read_enable:
            self.accepted.append(self.cycle)
        if returned is not None:
            self.returns.append((self.cycle, returned))
        self.cycle += 1
        return returned


class TestBurstFIFOStatusModel(unittest.TestCase):
    def test_read_acceptance_and_q_return_are_distinct_events(self):
        for latency in (2, 3, 8):
            with self.subTest(latency=latency):
                dut = FIFOReadReturnModel(response_latency=latency)
                for cycle in range(latency + 2):
                    dut.tick(cycle == 0, token="word0" if cycle == 0 else None)
                self.assertEqual(dut.accepted, [0])
                self.assertEqual(dut.returns, [(latency, "word0")])

    def test_one_bl8_word_then_idle_exposes_stale_empty_overread(self):
        # In RX_DATA_WIDTH=8 mode a BL8 can deliver one byte-wide FIFO word
        # per DQ bit slice.  The model intentionally puts one word in each
        # lane, then stops DQS.  Delayed EMPTY cannot count that word for us.
        for policy in ("common", "registered-common", "local"):
            with self.subTest(policy=policy):
                dut = BurstFIFOModel(2)
                for cycle in range(12):
                    dut.tick(((0, "A"), (1, "A")) if cycle == 0 else (), policy=policy)
                self.assertEqual(dut.pops, [["A"], ["A"]])
                self.assertGreater(sum(dut.underflows), 0)

    def test_lane_local_drain_breaks_alignment_when_one_lane_arrives_late(self):
        # Local status can start one byte lane before another.  The common
        # policy preserves one read edge for all lanes, although it still
        # cannot prevent stale-status over-read at the end of a short burst.
        local = BurstFIFOModel(2)
        common = BurstFIFOModel(2)
        for cycle in range(12):
            writes = ((0, "A"),) if cycle == 0 else (((1, "A"),) if cycle == 1 else ())
            local.tick(writes, policy="local")
            common.tick(writes, policy="common")
        # A lane-local design emits on different read-clock edges.
        self.assertNotEqual(local.pop_cycles[0][0], local.pop_cycles[1][0])
        # A shared enable consumes aligned words together once both EMPTY
        # indications arrive, while the primitive's delayed flags still
        # cause a later stale read in this short-burst model.
        self.assertEqual(common.pops[0], common.pops[1])
        self.assertGreater(sum(common.underflows), 0)


if __name__ == "__main__":
    unittest.main()
