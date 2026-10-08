#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Ordered lane assembly, backpressure and flush behavior with bounded FIFOs."""

import random
import unittest
from migen import Memory, Signal
from migen.sim import run_simulation
from litedram.phy.usnative.elastic import NativeFIFOReceiveAdapter, NativeReadAssembler


def simulate(dut, process):
    # This checkout's Migen FIFO creates write-only RAM ports, while its
    # simulator still expects dat_r on every port. Supply an unused read sink;
    # the FIFO memory, write enables and real synchronous read port are intact.
    fragment = dut.get_fragment()
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, process)


class TestNativeReadAssembler(unittest.TestCase):
    def test_skew_backpressure_and_full_rate(self):
        for lanes in (2, 4, 8):
            dut = NativeReadAssembler(lanes, depth=4)
            rng = random.Random(lanes)
            def process():
                sent = [0]*lanes
                received = 0
                consecutive = 0
                longest = 0
                for cycle in range(1600):
                    # First stress skew/backpressure, then demand full-rate flow.
                    full = cycle>=800
                    mask = sum(int(full or rng.randrange(4)!=0)<<i for i in range(lanes))
                    yield dut.lane_valid.eq(mask)
                    for i in range(lanes):
                        yield dut.lane_data[i].eq((sent[i]<<8)|i)
                    consume = full or rng.randrange(3)!=0
                    yield dut.ready.eq(consume)
                    yield
                    accepted = (yield dut.lane_ready)&mask
                    for i in range(lanes):
                        if accepted>>i&1:
                            sent[i]+=1
                    if (yield dut.valid) and consume:
                        value = (yield dut.data)
                        for i in range(lanes):
                            self.assertEqual((value>>(64*i))&((1<<64)-1), (received<<8)|i)
                        received+=1
                        consecutive+=1
                        longest = max(longest, consecutive)
                    else:
                        consecutive = 0
                self.assertGreater(received, 900)
                self.assertGreater(longest, 500)
            simulate(dut, process())

    def test_flush_blocks_handshakes_and_discards_partial_words(self):
        dut = NativeReadAssembler(8)
        def process():
            yield dut.lane_valid.eq(127)
            for i in range(8):
                yield dut.lane_data[i].eq(123+i)
            for _ in range(12):
                yield
            self.assertEqual((yield dut.valid), 0)
            self.assertEqual((yield dut.lane_ready)&127, 0)
            yield dut.flush.eq(1)
            yield dut.lane_valid.eq(255)
            yield dut.ready.eq(1)
            yield
            self.assertEqual((yield dut.valid), 0)
            self.assertEqual((yield dut.lane_ready), 0)
            yield
            yield dut.lane_valid.eq(0)
            yield dut.flush.eq(0)
            for _ in range(4):
                yield
            self.assertEqual((yield dut.valid), 0)
            self.assertEqual((yield dut.lane_ready), 255)
            for level in dut.level:
                self.assertEqual((yield level), 0)
        simulate(dut, process())


def adapter_layout(lanes):
    slices = []
    native_lanes = []
    for lane in range(lanes):
        base = lane * 10
        slices.extend(range(base, base + 10))
        native_lanes.append(type('Lane', (), dict(index=lane, dq=tuple(range(base, base+8)),
            strobe=base+8, mask=base+9))())
    return type('Layout', (), dict(slices=tuple(slices), lanes=tuple(native_lanes)))()


class TestNativeFIFOReceiveAdapter(unittest.TestCase):
    def test_elastic_valid_does_not_match_fixed_latency_dfi_valid_under_lane_skew(self):
        """Document why this adapter cannot yet replace the PHY's DFI path.

        LiteDRAM DFI has no downstream ready signal: the PHY promises
        ``rddata_valid`` at a fixed command-relative latency.  The elastic
        assembler's ``valid`` only reports that all byte lanes currently have
        a queued word.  A lane that becomes available later therefore stalls
        assembled output independently of the command schedule.  This test
        records that contract mismatch so the adapter is not connected to DFI
        until native FIFO return timing and retraining/flush behavior are
        specified end to end.
        """
        dut = NativeFIFOReceiveAdapter(adapter_layout(2), depth=4,
                                       response_latency=1)
        mismatch = []

        def process():
            yield dut.ready.eq(1)
            # The modeled command stream has a fixed read-valid window after
            # its command latency. Lane 1's native FIFO remains empty during
            # the beginning of that window, as can happen while DQS lanes
            # refill or are skewed at a burst boundary.
            for cycle in range(16):
                yield dut.lane_empty.eq(0b10 if cycle < 8 else 0)
                yield dut.lane_data[0].eq(0xA0 + cycle)
                yield dut.lane_data[1].eq(0xB0 + cycle)
                fixed_dfi_valid = 2 <= cycle < 12
                assembled_valid = (yield dut.valid)
                if fixed_dfi_valid and not assembled_valid:
                    mismatch.append(cycle)
                    break
                yield
            self.assertTrue(mismatch,
                'Lane skew did not expose the fixed-latency/elastic-valid mismatch')

        simulate(dut, process())

    def test_skewed_lanes_reserve_space_and_preserve_sequence_at_full_rate(self):
        lanes, latency, depth = 4, 2, 5
        dut = NativeFIFOReceiveAdapter(adapter_layout(lanes), depth=depth,
                                       response_latency=latency)
        rng = random.Random(0x8320)

        def process():
            received = 0
            issued = [0] * lanes
            max_run = run = issue_run = max_issue_run = 0
            expected_mask = (1 << len(adapter_layout(lanes).slices)) - 1
            for cycle in range(1800):
                # Independent EMPTY timing models lane skew. Once primed,
                # all lanes remain available to measure full-rate operation.
                for lane in range(lanes):
                    available = cycle >= 800 or rng.randrange(4) != 0
                    yield dut.lane_empty[lane].eq(not available)
                    yield dut.lane_data[lane].eq(lane)
                yield dut.ready.eq(1)
                mask = (yield dut.read_enable)
                valid = (yield dut.valid)
                value = (yield dut.data)
                yield
                for lane in range(lanes):
                    if mask & (1 << (lane*10)):
                        issued[lane] += 1
                if cycle >= 800 and mask == expected_mask:
                    issue_run += 1
                    max_issue_run = max(max_issue_run, issue_run)
                else:
                    issue_run = 0
                if valid:
                    for lane in range(lanes):
                        got = (value >> (64*lane)) & ((1 << 64)-1)
                        self.assertEqual(got, lane)
                    received += 1
                    if cycle >= 800:
                        run += 1
                        max_run = max(max_run, run)
                    else:
                        run = 0
                else:
                    run = 0
                for level in dut.lane_level:
                    self.assertLessEqual((yield level), depth)
            self.assertGreater(received, 700)
            self.assertGreater(max_run, 500)
            self.assertGreater(max_issue_run, 500)
            self.assertEqual(len(set(issued)), 1)

        simulate(dut, process())

    def test_flush_drains_responses_without_leaking_old_epoch(self):
        dut = NativeFIFOReceiveAdapter(adapter_layout(2), depth=4, response_latency=3)
        def process():
            yield dut.ready.eq(0)
            yield dut.lane_empty.eq(0)
            yield dut.lane_data[0].eq(0x111)
            yield dut.lane_data[1].eq(0x222)
            for _ in range(3):
                yield
            yield dut.flush.eq(1)
            yield
            yield dut.flush.eq(0)
            yield dut.lane_empty.eq(3)
            for _ in range(8):
                yield
                self.assertEqual((yield dut.valid), 0)
                self.assertEqual((yield dut.read_enable), 0)
            # New-epoch words can be read and assembled after the pipeline
            # drain; no pre-flush response remains in either lane queue.
            yield dut.lane_empty.eq(0)
            yield dut.lane_data[0].eq(0x331)
            yield dut.lane_data[1].eq(0x442)
            yield dut.ready.eq(1)
            for _ in range(12):
                yield
            self.assertEqual((yield dut.valid), 1)
            self.assertEqual((yield dut.data), (0x442 << 64) | 0x331)
        simulate(dut, process())
