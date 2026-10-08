#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Signal-level tests for the opt-in registered native FIFO scheduler."""

import unittest
from collections import deque

from migen import If, Module, Signal
from migen.sim import run_simulation

from litedram.phy.usnative.ddrphy import (
    _native_fifo_epoch_flush, _read_token_fifo_lane_drains,
    _registered_fifo_lane_drains,
)


class RegisteredDrainDUT(Module):
    def __init__(self, lanes):
        self.empty = Signal(lanes)
        self.ready = Signal()
        self.reset = Signal()
        available = [~self.empty[lane] for lane in range(lanes)]
        self.drains = _registered_fifo_lane_drains(
            self, available, self.ready, self.reset)


class TestUSNativeFIFOScheduler(unittest.TestCase):
    def test_bursts_gaps_and_lane_skew_stay_aligned_without_underflow(self):
        dut = RegisteredDrainDUT(2)

        def process():
            queues = [deque(), deque()]
            consumed = [[], []]
            underflows = [0, 0]
            # Lane 1 arrives late in each burst; gaps separate the bursts.
            arrivals = {
                0: ((0, "a0"),), 1: ((1, "a0"),),
                2: ((0, "a1"), (1, "a1")),
                7: ((0, "b0"),), 8: ((1, "b0"),),
                9: ((0, "b1"), (1, "b1")),
            }
            for cycle in range(22):
                for lane, token in arrivals.get(cycle, ()):
                    queues[lane].append(token)
                empty = sum((not queue) << lane for lane, queue in enumerate(queues))
                yield dut.empty.eq(empty)
                yield dut.ready.eq(1)
                yield dut.reset.eq(0)
                yield
                mask = 0
                for lane, drain in enumerate(dut.drains):
                    mask |= (yield drain) << lane
                self.assertIn(mask, (0, 0b11), "all byte lanes must drain together")
                for lane in range(2):
                    if mask & (1 << lane):
                        if queues[lane]:
                            consumed[lane].append(queues[lane].popleft())
                        else:
                            underflows[lane] += 1
            self.assertEqual(underflows, [0, 0])
            self.assertEqual(consumed, [["a0", "a1", "b0", "b1"]] * 2)

        run_simulation(dut, process())

    def test_reset_cancels_a_sampled_read(self):
        dut = RegisteredDrainDUT(2)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(0)
            yield
            # Availability was sampled; reset must discard the pending read.
            yield dut.reset.eq(1)
            yield
            mask = 0
            for lane, drain in enumerate(dut.drains):
                mask |= (yield drain) << lane
            self.assertEqual(mask, 0)
            yield dut.reset.eq(0)
            yield dut.empty.eq(0b11)
            for _ in range(4):
                yield
                mask = 0
                for lane, drain in enumerate(dut.drains):
                    mask |= (yield drain) << lane
                self.assertEqual(mask, 0)

        run_simulation(dut, process())


class ReadTokenDrainDUT(Module):
    def __init__(self, lanes):
        self.empty = Signal(lanes)
        self.read_issue = Signal()
        self.read_valid = Signal()
        self.ready = Signal()
        self.reset = Signal()
        self.epoch_flush = Signal()
        self.idle = Signal()
        available = [~self.empty[lane] for lane in range(lanes)]
        self.drains, self.selected_pops = _read_token_fifo_lane_drains(
            self, available, self.read_issue, self.read_valid,
            self.ready, self.reset, idle_status=self.idle,
            epoch_flush=self.epoch_flush)


class EpochFlushDUT(Module):
    def __init__(self):
        self.request = Signal()
        self.owner = Signal()
        self.ready = Signal()
        self.reset = Signal()
        self.scheduled_mode = Signal()
        self.token_idle = Signal()
        self.empty = Signal(8)
        self.count = Signal(32)
        (self.accepted, self.rd_en) = _native_fifo_epoch_flush(
            self, self.request, self.owner, self.ready, self.reset,
            self.scheduled_mode, self.token_idle, self.empty,
            (0, 2, 4, 7), self.count)


class CombinedEpochDrainDUT(Module):
    """Small top-level-equivalent wiring for FIFO flush and token drains."""
    def __init__(self):
        self.empty = Signal(13)
        self.request = Signal()
        self.owner = Signal()
        self.ready = Signal()
        self.reset = Signal()
        self.scheduled_mode = Signal()
        self.read_issue = Signal()
        self.read_valid = Signal()
        self.read_en = Signal(13)
        self.accepted_count = Signal(32)
        self.fifo_reads = [Signal(32, name=f'fifo_reads{lane}')
                           for lane in range(2)]
        dq_lanes = ((0, 1, 2, 3), (4, 5, 6, 7))
        lane_available = [~(self.empty[dq[0]] | self.empty[dq[1]] |
                            self.empty[dq[2]] | self.empty[dq[3]])
                          for dq in dq_lanes]
        token_idle = Signal()
        epoch_accepted, epoch_reads = _native_fifo_epoch_flush(
            self, self.request, self.owner, self.ready, self.reset,
            self.scheduled_mode, token_idle, self.empty,
            tuple(range(12)), self.accepted_count)
        self.token_drains, self.selected_pops = _read_token_fifo_lane_drains(
            self, lane_available, self.read_issue, self.read_valid,
            self.ready, self.reset | self.scheduled_mode,
            idle_status=token_idle, epoch_flush=epoch_accepted)
        # Match the PHY's OR of the ordinary lane drain and explicit
        # per-tap epoch pulse. The small fixture uses four DQ taps per byte;
        # tap 12 stands in for an unmapped/control resource.
        for lane, taps in enumerate((range(0, 4), range(4, 8))):
            for tap in tuple(taps) + (8 + lane, 10 + lane):
                self.comb += self.read_en[tap].eq(
                    (self.token_drains[lane] & self.ready) | epoch_reads[tap])
            self.sync += If(self.selected_pops[lane],
                self.fifo_reads[lane].eq(self.fifo_reads[lane] + 1))
        self.comb += self.read_en[12].eq(0)


class TestUSNativeEpochFlush(unittest.TestCase):
    def test_single_pulse_only_reads_nonempty_mapped_data_taps(self):
        dut = EpochFlushDUT()
        observed = []

        def process():
            yield dut.owner.eq(1)
            yield dut.ready.eq(1)
            yield dut.token_idle.eq(1)
            # 0, 2 and 7 are mapped data taps and nonempty. Tap 5 is also
            # nonempty but represents an unmapped clock/control resource.
            yield dut.empty.eq(0xff & ~(1 << 0) & ~(1 << 2) & ~(1 << 5) & ~(1 << 7))
            yield dut.request.eq(1)
            yield
            observed.append(((yield dut.accepted), (yield dut.rd_en)))
            yield dut.request.eq(0)
            for _ in range(3):
                yield
            observed.append(((yield dut.accepted), (yield dut.rd_en),
                             (yield dut.count)))

        run_simulation(dut, process())
        self.assertEqual(observed[0], (1, 0b10000101))
        self.assertEqual(observed[1], (0, 0, 1))

    def test_unsafe_owner_readiness_reset_scheduled_or_busy_requests_are_dropped(self):
        dut = EpochFlushDUT()
        cases = (
            (0, 1, 0, 0, 1),  # no software ownership
            (1, 0, 0, 0, 1),  # not ready
            (1, 1, 1, 0, 1),  # reset
            (1, 1, 0, 1, 1),  # scheduled FIFO mode owns reads
            (1, 1, 0, 0, 0),  # a READ is outstanding
        )
        accepted = []

        def process():
            yield dut.empty.eq(0)
            for owner, ready, reset, scheduled, idle in cases:
                yield dut.owner.eq(owner)
                yield dut.ready.eq(ready)
                yield dut.reset.eq(reset)
                yield dut.scheduled_mode.eq(scheduled)
                yield dut.token_idle.eq(idle)
                yield dut.request.eq(1)
                yield
                accepted.append((yield dut.accepted))
                yield dut.request.eq(0)
                yield dut.reset.eq(1)
                yield
                yield dut.reset.eq(0)
            self.assertEqual((yield dut.count), 0)

        run_simulation(dut, process())
        self.assertEqual(accepted, [0] * len(cases))

    def test_integrated_flush_drains_partial_taps_once_across_delayed_empty(self):
        dut = CombinedEpochDrainDUT()
        pulses = []
        queue = [0] * 13
        # Lane 0 has a full DQ word; lane 1 has only one stale DQ FIFO word.
        # DQS and DM carry independent partial occupancy, while tap 12 is an
        # unmapped/control FIFO that must never be touched by epoch cleanup.
        for tap in (0, 1, 2, 3, 4, 8, 9, 11, 12):
            queue[tap] = 1
        actual_empty = sum((not words) << tap for tap, words in enumerate(queue))
        empty_history = [actual_empty, actual_empty]
        underflows = 0

        def process():
            nonlocal actual_empty, empty_history, underflows
            yield dut.owner.eq(1)
            yield dut.ready.eq(1)
            for cycle in range(7):
                # FIFO_EMPTY is deliberately two edges behind occupancy.
                yield dut.empty.eq(empty_history[0])
                yield dut.request.eq(cycle == 0)
                yield
                rd_en = (yield dut.read_en)
                accepted = (yield dut.accepted_count)
                selected = 0
                fifo_reads = []
                for lane in range(2):
                    selected |= (yield dut.selected_pops[lane]) << lane
                    fifo_reads.append((yield dut.fifo_reads[lane]))
                fifo_reads = tuple(fifo_reads)
                pulses.append((rd_en, accepted, selected, fifo_reads))
                for tap in range(13):
                    if rd_en & (1 << tap):
                        if queue[tap]:
                            queue[tap] -= 1
                        else:
                            underflows += 1
                actual_empty = sum((not words) << tap
                                   for tap, words in enumerate(queue))
                empty_history = [empty_history[1], actual_empty]

            self.assertEqual((yield dut.read_issue), 0)

        run_simulation(dut, process())
        self.assertEqual(pulses[0][0], sum(1 << tap for tap in
            (0, 1, 2, 3, 4, 8, 9, 11)))
        self.assertEqual(pulses[0][1:], (0, 0, (0, 0)))
        self.assertEqual(pulses[1][1], 1, "accepted-request count must advance once")
        self.assertTrue(all(row[1] == 1 for row in pulses[1:]), pulses)
        self.assertTrue(all(row[0] == 0 for row in pulses[1:]), pulses)
        self.assertTrue(all(row[2:] == (0, (0, 0)) for row in pulses), pulses)
        self.assertEqual(underflows, 0)
        self.assertEqual([queue[tap] for tap in range(12)], [0] * 12)
        self.assertEqual(queue[12], 1, "unmapped/control occupancy must be preserved")

    def test_late_expired_word_can_alias_next_read_before_empty_visibility(self):
        dut = ReadTokenDrainDUT(1)
        observed = []

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            yield dut.read_issue.eq(1)
            yield
            yield dut.read_issue.eq(0)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            # A late word is written now, but the read-domain EMPTY flag stays
            # high for two edges. A new command before visibility sees the
            # old high flag, arms saw_empty, and can consume that stale word.
            yield dut.read_issue.eq(1)
            yield
            observed.append(((yield dut.drains[0]),
                             (yield dut.selected_pops[0])))
            yield dut.read_issue.eq(0)
            yield dut.empty.eq(0)
            yield
            observed.append(((yield dut.drains[0]),
                             (yield dut.selected_pops[0])))

        run_simulation(dut, process())
        self.assertEqual(observed, [(0, 0), (1, 1)])

    def test_quiet_interval_idle_flushes_late_word_before_next_read(self):
        dut = ReadTokenDrainDUT(1)
        observed = []

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            yield dut.read_issue.eq(1)
            yield
            yield dut.read_issue.eq(0)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            # Model delayed write visibility after the valid edge, then hold
            # off READ B long enough for the unqualified idle path to remove
            # the late word before the EMPTY deassertion is trusted.
            yield
            yield dut.empty.eq(0)
            yield
            observed.append(((yield dut.drains[0]),
                             (yield dut.selected_pops[0])))
            yield dut.empty.eq(1)
            yield
            yield
            yield dut.read_issue.eq(1)
            yield
            observed.append(((yield dut.drains[0]),
                             (yield dut.selected_pops[0])))

        run_simulation(dut, process())
        self.assertEqual(observed, [(1, 0), (0, 0)])

    def test_two_edge_cooldown_rejects_extra_pulses(self):
        dut = EpochFlushDUT()
        accepted = []

        def process():
            yield dut.owner.eq(1)
            yield dut.ready.eq(1)
            yield dut.token_idle.eq(1)
            yield dut.empty.eq(0)
            yield dut.request.eq(1)
            yield
            accepted.append((yield dut.accepted))
            yield dut.request.eq(0)
            yield dut.request.eq(1)
            yield
            accepted.append((yield dut.accepted))  # first edge after pulse
            yield
            accepted.append((yield dut.accepted))  # second edge after pulse
            yield dut.request.eq(0)
            yield
            yield dut.request.eq(1)
            yield
            accepted.append((yield dut.accepted))
            yield
            self.assertEqual((yield dut.count), 2)

        run_simulation(dut, process())
        self.assertEqual(accepted, [1, 0, 0, 1])


class TestUSNativeReadTokenDrain(unittest.TestCase):
    def test_idle_status_includes_current_and_outstanding_read_activity(self):
        dut = ReadTokenDrainDUT(1)
        observed = []

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            yield
            observed.append((yield dut.idle))
            yield dut.read_issue.eq(1)
            yield
            observed.append((yield dut.idle))
            yield dut.read_issue.eq(0)
            yield
            observed.append((yield dut.idle))
            yield dut.read_valid.eq(1)
            yield
            observed.append((yield dut.idle))
            yield dut.read_valid.eq(0)
            yield
            observed.append((yield dut.idle))

        run_simulation(dut, process())
        self.assertEqual(observed, [1, 0, 0, 0, 1])

    def test_epoch_flush_spaces_idle_pops_but_not_read_token_pops(self):
        dut = ReadTokenDrainDUT(1)
        observed = []

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(0)
            yield dut.epoch_flush.eq(1)
            self.assertEqual((yield dut.drains[0]), 0)
            yield
            yield dut.epoch_flush.eq(0)
            for _ in range(2):
                observed.append((yield dut.drains[0]))
                yield
            observed.append((yield dut.drains[0]))
            # Start a qualified READ during the idle-flush wait interval.
            yield dut.empty.eq(1)
            yield dut.read_issue.eq(1)
            yield
            yield dut.read_issue.eq(0)
            yield dut.empty.eq(0)
            yield
            observed.append(((yield dut.drains[0]),
                             (yield dut.selected_pops[0])))
            yield
            observed.append(((yield dut.drains[0]),
                             (yield dut.selected_pops[0])))

        run_simulation(dut, process())
        self.assertEqual(observed[:3], [0, 0, 0])
        self.assertEqual(observed[3], (1, 1))
        self.assertEqual(observed[4], (0, 0))

    def test_depth_16_rejects_seventeenth_read_without_a_valid_retirement(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            for _ in range(16):
                yield dut.read_issue.eq(1)
                yield
            # No retirement is available, so the full queue must reject this
            # pulse rather than wrap the five-bit count or overwrite a slot.
            yield dut.read_issue.eq(1)
            yield
            yield dut.read_issue.eq(0)
            for _ in range(16):
                yield dut.read_valid.eq(1)
                yield
                yield dut.read_valid.eq(0)
                yield
            # All accepted allowances expired. A later word is only an idle
            # flush, not a selected pop for an accidentally accepted READ 17.
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 0)
            self.assertEqual((yield dut.drains[0]), 1)

        run_simulation(dut, process())

    def test_full_queue_accepts_replacement_on_simultaneous_retire(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            for _ in range(16):
                yield dut.read_issue.eq(1)
                yield
            # A full queue may accept exactly one new request when the oldest
            # request retires on this same edge.
            yield dut.read_issue.eq(1)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_issue.eq(0)
            yield dut.read_valid.eq(0)
            for _ in range(15):
                yield dut.read_valid.eq(1)
                yield
                yield dut.read_valid.eq(0)
                yield
            # The original sixteen requests have retired; the replacement is
            # still represented and can claim its lane token.
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 1)
            self.assertEqual((yield dut.drains[0]), 1)
            yield dut.empty.eq(1)
            yield
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 0)
            self.assertEqual((yield dut.drains[0]), 1)

        run_simulation(dut, process())

    def test_pop_on_missing_oldest_valid_edge_is_consumed_once(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            for _ in range(2):
                yield dut.read_issue.eq(1)
                yield
            yield dut.read_issue.eq(0)
            # No word has arrived for the oldest request before its valid edge.
            yield dut.empty.eq(0)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield dut.empty.eq(1)
            yield
            # The same-edge pop satisfies the retiring oldest request exactly
            # once; the second READ must retain its own allowance.
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 1)
            self.assertEqual((yield dut.drains[0]), 1)
            yield dut.empty.eq(1)
            yield
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 0)
            self.assertEqual((yield dut.drains[0]), 1)

        run_simulation(dut, process())

    def test_lane_local_oldest_policy_cannot_detect_cross_lane_read_identity(self):
        dut = ReadTokenDrainDUT(2)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(0b11)
            for _ in range(2):
                yield dut.read_issue.eq(1)
                yield
            yield dut.read_issue.eq(0)
            # The test labels this as a possible newer-read arrival, but the
            # hardware supplies no read tag. Lane 0 necessarily credits it to
            # its oldest unmatched slot; lane 1 still has no token.
            yield dut.empty.eq(0b10)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 1)
            self.assertEqual((yield dut.selected_pops[1]), 0)
            yield dut.empty.eq(0b11)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            # Another lane-0 word is assigned to its remaining unmatched
            # request, but the helper cannot establish whether either word
            # physically belonged to the old or new READ.
            yield dut.empty.eq(0b10)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 1)
            self.assertEqual((yield dut.selected_pops[1]), 0)

        run_simulation(dut, process())

    def test_nonempty_at_issue_is_rejected_until_empty_is_observed(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(0)
            yield dut.read_issue.eq(1)
            yield
            self.assertEqual((yield dut.drains[0]), 0)
            yield dut.read_issue.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 0)
            yield dut.empty.eq(1)
            yield
            self.assertEqual((yield dut.drains[0]), 0)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            self.assertEqual((yield dut.selected_pops[0]), 1)

        run_simulation(dut, process())

    def test_idle_flush_waits_for_two_cycle_empty_visibility(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            # Model one stale FIFO word. After its pop, EMPTY remains low for
            # two complete read edges, then reflects the now-empty FIFO.
            yield dut.empty.eq(0)
            yield
            observed = []
            for cycle in range(4):
                if cycle == 2:
                    yield dut.empty.eq(1)
                pop = (yield dut.drains[0])
                observed.append(pop)
                yield
            self.assertEqual(observed, [1, 0, 0, 0])

        run_simulation(dut, process())

    def test_multiple_idle_words_are_flushed_with_empty_latency_spacing(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(0)
            yield
            remaining = 3
            empty_visible = 0
            empty_delay = 0
            pop_cycles = []
            # Model the FIFO's read-domain EMPTY status: it stays low while
            # words remain, and takes two read edges to rise after the final
            # pop. The lane helper must pace unqualified pops at cycles 0, 3,
            # and 6, then stop even while the flag is settling.
            for cycle in range(11):
                # Assert the modeled status one generator phase before the
                # sample edge at which its two-clock synchronizer completes.
                yield dut.empty.eq(empty_visible or (empty_delay == 1))
                pop = (yield dut.drains[0])
                if pop:
                    self.assertGreater(remaining, 0, "must not read past EMPTY")
                    pop_cycles.append(cycle)
                yield
                if pop:
                    remaining -= 1
                    if remaining == 0:
                        empty_delay = 2
                elif empty_delay:
                    empty_delay -= 1
                    if empty_delay == 0:
                        empty_visible = 1
            self.assertEqual(pop_cycles, [0, 3, 6])
            self.assertEqual(remaining, 0)

        run_simulation(dut, process())

    def test_consecutive_qualified_token_pops_ignore_idle_flush_wait(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            pops = []
            for _ in range(2):
                yield dut.read_issue.eq(1)
                yield
                pops.append((yield dut.drains[0]))
            yield dut.read_issue.eq(0)
            # Two actual READ tokens arrive in adjacent read-clock cycles.
            # The first starts the idle-flush cooldown; the second qualified
            # token pop must still be accepted immediately.
            yield dut.empty.eq(0)
            yield
            pops.append((yield dut.drains[0]))
            yield
            pops.append((yield dut.drains[0]))
            self.assertEqual(pops[-2:], [1, 1])

        run_simulation(dut, process())

    def test_reset_clears_idle_flush_wait(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            yield
            # The preceding idle pop created cooldown. Reset clears it while
            # suppressing reads; after reset, a fresh stale word can flush.
            yield dut.reset.eq(1)
            self.assertEqual((yield dut.drains[0]), 0)
            yield
            yield dut.reset.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)

        run_simulation(dut, process())

    def test_stale_word_is_flushed_before_read_and_selected_word_is_held(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            self.assertEqual((yield dut.selected_pops[0]), 0)
            yield dut.empty.eq(1)
            yield dut.read_issue.eq(1)
            yield
            self.assertEqual((yield dut.drains[0]), 0)
            yield dut.read_issue.eq(0)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            self.assertEqual((yield dut.selected_pops[0]), 1)
            yield
            # A second word is visible, but must not overwrite the selected
            # word before the fixed-latency read-valid edge.
            self.assertEqual((yield dut.drains[0]), 0)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            self.assertEqual((yield dut.selected_pops[0]), 0)

        run_simulation(dut, process())

    def test_missing_read_expires_before_late_word_and_next_read(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            yield dut.read_issue.eq(1)
            yield
            yield dut.read_issue.eq(0)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            # A word arriving after its READ valid edge is idle-flushed; it
            # must not consume the allowance for the subsequent command.
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            self.assertEqual((yield dut.selected_pops[0]), 0)
            yield dut.empty.eq(1)
            yield
            yield dut.read_issue.eq(1)
            yield
            self.assertEqual((yield dut.drains[0]), 0)
            yield dut.read_issue.eq(0)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            self.assertEqual((yield dut.selected_pops[0]), 1)
            yield dut.empty.eq(1)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 0)

        run_simulation(dut, process())

    def test_simultaneous_issue_pop_and_valid_retires_old_and_keeps_new(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            yield dut.read_issue.eq(1)
            yield
            yield
            yield dut.read_issue.eq(0)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 1)
            # The next word pop coincides with the oldest read-valid and a
            # new issue. The old request retires, while the new allowance remains.
            yield dut.read_issue.eq(1)
            yield dut.read_valid.eq(1)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            self.assertEqual((yield dut.selected_pops[0]), 1)
            yield dut.read_issue.eq(0)
            yield dut.read_valid.eq(0)
            yield dut.empty.eq(1)
            yield
            self.assertEqual((yield dut.selected_pops[0]), 0)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 0)

        run_simulation(dut, process())

    def test_back_to_back_reads_allow_two_words(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            pops = 0
            for _ in range(2):
                yield dut.read_issue.eq(1)
                yield
                pops += (yield dut.drains[0])
            yield dut.read_issue.eq(0)
            yield dut.empty.eq(0)
            yield
            pops += (yield dut.drains[0])
            yield
            pops += (yield dut.drains[0])
            self.assertEqual(pops, 2)
            yield
            self.assertEqual((yield dut.drains[0]), 0)

        run_simulation(dut, process())

    def test_lane_skew_retains_separate_read_allowances(self):
        dut = ReadTokenDrainDUT(2)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(0b11)
            for _ in range(2):
                yield dut.read_issue.eq(1)
                yield
                self.assertEqual((yield dut.drains[0]), 0)
                self.assertEqual((yield dut.drains[1]), 0)
            yield dut.read_issue.eq(0)
            yield dut.empty.eq(0b10)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            self.assertEqual((yield dut.drains[1]), 0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            yield dut.empty.eq(0b00)
            yield
            self.assertEqual((yield dut.drains[0]), 0)
            self.assertEqual((yield dut.drains[1]), 1)
            yield
            self.assertEqual((yield dut.drains[1]), 1)
            yield
            self.assertEqual((yield dut.drains[1]), 0)

        run_simulation(dut, process())

    def test_reset_and_not_ready_flush_outstanding_tokens(self):
        dut = ReadTokenDrainDUT(1)

        def process():
            yield dut.ready.eq(1)
            yield dut.empty.eq(1)
            yield dut.read_issue.eq(1)
            yield
            yield dut.read_issue.eq(0)
            yield dut.ready.eq(0)
            yield
            yield dut.ready.eq(1)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)
            yield dut.read_issue.eq(1)
            yield dut.empty.eq(1)
            yield
            yield dut.read_issue.eq(0)
            yield dut.reset.eq(1)
            yield dut.empty.eq(0)
            yield dut.read_issue.eq(1)
            yield
            self.assertEqual((yield dut.drains[0]), 0)
            yield dut.reset.eq(0)
            yield dut.read_issue.eq(0)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.drains[0]), 1)

        run_simulation(dut, process())

if __name__ == "__main__":
    unittest.main()
