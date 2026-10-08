#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

import sys
import random
import unittest
from migen import Module, Memory, Signal, If
from migen.sim import run_simulation, passive
from litedram.common import LiteDRAMNativePort
from litex.soc.interconnect import stream
from test.common import DRAMMemory
from litedram.frontend.native_benchmark import NativeDMABenchmark

def simulate(dut, generators):
    fragment = dut.get_fragment()
    # The pinned simulator's MemoryToArray assumes every port has dat_r,
    # whereas the current buffered FIFO creates write-only memory ports.
    # Add unused read wires only to the simulation fragment; RTL is unchanged.
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, generators)

class DMATest(unittest.TestCase):
    def test_one_beat_per_cycle_with_outstanding_reads(self):
        wp = LiteDRAMNativePort('write', 10, 128)
        rp = LiteDRAMNativePort('read', 10, 128)
        top = Module()
        top.submodules.dut = dut = NativeDMABenchmark(wp, rp, capacity=16384)
        memory = Memory(128, 1024)
        wr = memory.get_port(write_capable=True)
        rd = memory.get_port(async_read=True)
        top.specials += memory, wr, rd
        top.submodules.addresses = addresses = stream.SyncFIFO([('address', 10)], 64)
        top.comb += [addresses.sink.valid.eq(wp.cmd.valid), addresses.sink.address.eq(wp.cmd.addr),
            wp.cmd.ready.eq(addresses.sink.ready), wp.wdata.ready.eq(addresses.source.valid),
            addresses.source.ready.eq(wp.wdata.valid), wr.adr.eq(addresses.source.address),
            wr.dat_w.eq(wp.wdata.data), wr.we.eq(wp.wdata.valid & wp.wdata.ready),
            rp.cmd.ready.eq(1), rd.adr.eq(rp.cmd.addr)]
        pipeline = [Signal(128) for _ in range(16)]
        valid = [Signal() for _ in range(16)]
        top.sync += [pipeline[0].eq(rd.dat_r), valid[0].eq(rp.cmd.valid & rp.cmd.ready)]
        for i in range(1, 16):
            top.sync += [pipeline[i].eq(pipeline[i-1]), valid[i].eq(valid[i-1])]
        top.comb += [rp.rdata.data.eq(pipeline[-1]), rp.rdata.valid.eq(valid[-1])]
        def main():
            yield dut.allowed.eq(1)
            yield dut._base.storage.eq(0)
            yield dut._length.storage.eq(4096)
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            for _ in range(2000):
                if (yield dut._done.status):
                    break
                yield
            self.assertEqual((yield dut._done.status), 1)
            self.assertEqual((yield dut._fault.status), 0)
            self.assertEqual((yield dut._errors.status), 0)
            self.assertEqual((yield dut._write_beats.status), 256)
            self.assertEqual((yield dut._read_beats.status), 256)
            self.assertLessEqual((yield dut._write_cycles.status), 264)
            self.assertLessEqual((yield dut._read_cycles.status), 280)
        simulate(top, main())

    def run_case(self, random_data=1, corrupt=False, width=128):
        beat_bytes = width//8
        beats = 1024//beat_bytes
        first = 64//beat_bytes
        last = first+beats-1
        wp = LiteDRAMNativePort('write', address_width=10, data_width=width)
        rp = LiteDRAMNativePort('read', address_width=10, data_width=width)
        dut = NativeDMABenchmark(wp, rp, capacity=16384, fifo_depth=8, databits=16)
        memory = DRAMMemory(width, 1024)
        def start(read_only=0):
            yield dut._read_only.storage.eq(read_only)
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            yield
        def wait():
            for _ in range(10000):
                if (yield dut._done.status):
                    return
                yield
            self.fail('DMA did not finish')
        def main():
            yield dut.allowed.eq(1)
            yield dut._base.storage.eq(64)
            yield dut._length.storage.eq(1024)
            yield dut._random.storage.eq(random_data)
            yield from start()
            yield from wait()
            self.assertEqual((yield dut._fault.status), 0)
            self.assertEqual((yield dut._errors.status), 0)
            self.assertEqual((yield dut._write_beats.status), beats)
            self.assertEqual((yield dut._read_beats.status), beats)
            self.assertGreaterEqual((yield dut._write_cycles.status), beats)
            self.assertGreaterEqual((yield dut._read_cycles.status), beats)
            self.assertEqual(memory.mem[:first], [0]*first)
            self.assertEqual(memory.mem[last+1:], [0]*(1024-last-1))
            if corrupt:
                memory.mem[last]^=1<<100  # final beat must also be checked
            yield from start(1)
            yield from wait()
            self.assertEqual((yield dut._fault.status), 0)
            self.assertEqual((yield dut._write_beats.status), 0)
            self.assertEqual((yield dut._read_beats.status), beats)
            self.assertEqual((yield dut._errors.status), int(corrupt))
            if corrupt:
                self.assertEqual((yield dut._first_error_offset.status), last*beat_bytes)
                self.assertEqual((yield dut._first_error_xor.status), 1<<100)
                self.assertEqual((yield dut._dq_error_mask.status), 1<<(100%16))
            # An invalid request must issue no commands and allow a later valid run.
            yield dut._length.storage.eq(0)
            yield from start()
            yield from wait()
            self.assertEqual((yield dut._fault.status), 1)
            self.assertEqual((yield dut._write_beats.status), 0)
            yield dut._length.storage.eq(beat_bytes)
            yield from start()
            yield from wait()
            self.assertEqual((yield dut._fault.status), 0)
            self.assertEqual((yield dut._errors.status), 0)
            self.assertEqual((yield dut._read_beats.status), 1)
        simulate(dut, [main(), memory.write_handler(wp, wdata_ready_random=60),
                            memory.read_handler(rp, rdata_valid_random=60)])
    def test_counter_backpressure(self):
        self.run_case(0)
    def test_prbs_backpressure_and_corruption(self):
        self.run_case(1, True)

    def test_prbs_skip_aligns_shifted_base(self):
        width = 128
        beat_bytes = width // 8
        wp = LiteDRAMNativePort('write', address_width=10, data_width=width)
        rp = LiteDRAMNativePort('read', address_width=10, data_width=width)
        dut = NativeDMABenchmark(wp, rp, capacity=16384, fifo_depth=8)
        memory = DRAMMemory(width, 1024)

        def start(read_only=0):
            yield dut._read_only.storage.eq(read_only)
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            yield

        def wait():
            for _ in range(10000):
                if (yield dut._done.status):
                    return
                yield
            self.fail('DMA did not finish')

        def main():
            yield dut.allowed.eq(1)
            yield dut._random.storage.eq(1)
            # Reference sequence: PRBS beat zero is stored at physical beat 0.
            yield dut._base.storage.eq(0)
            yield dut._length.storage.eq(256)
            yield dut._skip_beats.storage.eq(0)
            yield from start()
            yield from wait()
            self.assertEqual((yield dut._errors.status), 0)
            reference = list(memory.mem[:16])

            # Move the region forward four physical beats and skip the same
            # four PRBS beats. The overlapping physical addresses must retain
            # exactly the same expected data.
            yield dut._base.storage.eq(4 * beat_bytes)
            yield dut._length.storage.eq(192)
            yield dut._skip_beats.storage.eq(4)
            yield from start()
            yield from wait()
            self.assertEqual((yield dut._fault.status), 0)
            self.assertEqual((yield dut._errors.status), 0)
            self.assertEqual(memory.mem[4:16], reference[4:16])

            # Verify the read-side PRBS generator also receives the skip: a
            # read-only check of the shifted region must compare cleanly.
            memory.mem[8] ^= 1
            yield from start(read_only=1)
            yield from wait()
            self.assertEqual((yield dut._errors.status), 1)
            self.assertEqual((yield dut._first_error_offset.status), 8 * beat_bytes)

        simulate(dut, [main(), memory.write_handler(wp, wdata_ready_random=40),
                       memory.read_handler(rp, rdata_valid_random=40)])

    def test_skip_obeys_timeout_and_readiness_guard(self):
        wp = LiteDRAMNativePort('write', 10, 128)
        rp = LiteDRAMNativePort('read', 10, 128)
        dut = NativeDMABenchmark(wp, rp, capacity=16384)

        def main():
            yield dut.allowed.eq(1)
            yield dut._base.storage.eq(0)
            yield dut._length.storage.eq(16)
            yield dut._skip_beats.storage.eq(100)
            yield dut._timeout.storage.eq(3)
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            for _ in range(10):
                if (yield dut._done.status):
                    break
                yield
            self.assertEqual((yield dut._done.status), 1)
            self.assertEqual((yield dut._fault.status), 3)
            self.assertEqual((yield dut._write_beats.status), 0)
            self.assertEqual((yield dut._read_beats.status), 0)

        simulate(dut, main())
    def test_256_bit_counter(self):
        self.run_case(0, width=256)
    def test_256_bit_prbs_corruption(self):
        self.run_case(1, True, width=256)

    def test_256_bit_first_error_capture(self):
        width = 256
        beat_bytes = width // 8
        base = 64
        beats = 1024 // beat_bytes
        first = base // beat_bytes
        error_index = first + 1
        error_xor = sum(1 << (24 + 31*i) for i in range(8))
        wp = LiteDRAMNativePort('write', address_width=10, data_width=width)
        rp = LiteDRAMNativePort('read', address_width=10, data_width=width)
        dut = NativeDMABenchmark(wp, rp, capacity=16384, fifo_depth=8,
            databits=16, capture_first_error=True)
        memory = DRAMMemory(width, 1024)

        def start(read_only=0):
            yield dut._read_only.storage.eq(read_only)
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            yield

        def wait():
            for _ in range(10000):
                if (yield dut._done.status):
                    return
                yield
            self.fail('DMA did not finish')

        def main():
            yield dut.allowed.eq(1)
            yield dut._base.storage.eq(base)
            yield dut._length.storage.eq(1024)
            yield dut._random.storage.eq(0)
            yield from start()
            yield from wait()
            expected = memory.mem[error_index]
            memory.mem[error_index] ^= error_xor
            yield from start(1)
            yield from wait()
            self.assertEqual((yield dut._errors.status), 1)
            self.assertEqual((yield dut._first_error_offset.status), error_index * beat_bytes)
            self.assertEqual((yield dut._first_error_xor.status), error_xor)
            self.assertEqual((yield dut._first_error_actual.status), expected ^ error_xor)
            self.assertEqual((yield dut._first_error_expected.status), expected)

        simulate(dut, [main(), memory.write_handler(wp, wdata_ready_random=60),
                            memory.read_handler(rp, rdata_valid_random=60)])

    def test_repeat_read_has_independent_results_and_dwell(self):
        width = 128
        beat_bytes = width // 8
        base = 64
        beats = 128 // beat_bytes
        first_bad = 2
        wp = LiteDRAMNativePort('write', 10, width)
        rp = LiteDRAMNativePort('read', 10, width)
        dut = NativeDMABenchmark(wp, rp, capacity=16384, fifo_depth=8,
            databits=16, capture_first_error=True)
        repeat_start_count = Signal(8)
        dut.sync += If(dut.repeat_read_start,
            repeat_start_count.eq(repeat_start_count + 1))
        memory = DRAMMemory(width, 1024)

        def main():
            yield dut.allowed.eq(1)
            yield dut._base.storage.eq(base)
            yield dut._length.storage.eq(beats * beat_bytes)
            yield dut._skip_beats.storage.eq(1)
            # Seed a valid PRBS region, then inject one stable corruption.
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            for _ in range(1000):
                if (yield dut._done.status):
                    break
                yield
            self.assertEqual((yield dut._errors.status), 0)
            bad_expected = memory.mem[base // beat_bytes + first_bad]
            memory.mem[base // beat_bytes + first_bad] ^= 1
            yield dut._read_only.storage.eq(1)
            yield dut._repeat_verify.storage.eq(1)
            yield dut._repeat_idle_cycles.storage.eq(7)
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            # The first pass has one error. While its configured dwell runs,
            # the repeat counters remain untouched and the command stays busy.
            for _ in range(1000):
                if (yield dut._errors.status):
                    break
                yield
            self.assertEqual((yield dut._errors.status), 1)
            self.assertEqual((yield dut._busy.status), 1)
            self.assertEqual((yield dut._repeat_read_beats.status), 0)
            for _ in range(6):
                yield
                self.assertEqual((yield dut._busy.status), 1)
                self.assertEqual((yield dut._repeat_read_beats.status), 0)
            for _ in range(100):
                if (yield dut._repeat_read_beats.status):
                    break
                yield
            first_read_cycles = (yield dut._read_cycles.status)
            first_read_stalls = (yield dut._read_stalls.status)
            for _ in range(1000):
                if (yield dut._done.status):
                    break
                yield
            self.assertEqual((yield dut._fault.status), 0)
            self.assertEqual((yield dut._read_beats.status), beats)
            self.assertEqual((yield dut._repeat_read_beats.status), beats)
            self.assertEqual((yield dut._read_cycles.status), first_read_cycles)
            self.assertEqual((yield dut._read_stalls.status), first_read_stalls)
            self.assertEqual((yield dut._errors.status), 1)
            self.assertEqual((yield dut._first_error_offset.status), base + first_bad*beat_bytes)
            self.assertEqual((yield dut._first_error_xor.status), 1)
            self.assertEqual((yield dut._first_error_actual.status), bad_expected ^ 1)
            self.assertEqual((yield dut._first_error_expected.status), bad_expected)
            self.assertEqual((yield dut._repeat_errors.status), 1)
            self.assertEqual((yield dut._repeat_first_error_offset.status), base + first_bad*beat_bytes)
            self.assertEqual((yield dut._repeat_first_error_xor.status), 1)
            self.assertEqual((yield dut._repeat_first_error_actual.status), bad_expected ^ 1)
            self.assertEqual((yield dut._repeat_first_error_expected.status), bad_expected)
            self.assertEqual((yield repeat_start_count), 1)

        simulate(dut, [main(), memory.write_handler(wp), memory.read_handler(rp)])

    def test_512_bit_counter_repeat_seeded_near_4mib_and_8mib_boundaries(self):
        width = 512
        beat_bytes = width // 8
        injected_beat = 2
        injected_xor = 1 << 15

        def counter_payload(value):
            # Independent software model of the 31-bit incrementing word,
            # repeated across the physical 512-bit DMA beat.
            value &= (1 << 31) - 1
            payload = sum(value << (31 * copy) for copy in range(17))
            return payload & ((1 << width) - 1)

        for boundary in (4 << 20, 8 << 20):
            with self.subTest(boundary=boundary):
                base = boundary - 2 * beat_bytes
                base_word = base // beat_bytes
                first_counter = base_word
                beats = 4
                wp = LiteDRAMNativePort('write', address_width=18, data_width=width)
                rp = LiteDRAMNativePort('read', address_width=18, data_width=width)
                dut = NativeDMABenchmark(wp, rp, capacity=1 << 30,
                    fifo_depth=8, databits=64, capture_first_error=True)
                in_write = dut.fsm.ongoing('WRITE')
                in_repeat_read = dut.fsm.ongoing('REPEAT_READ')

                class CorruptSecondRead(DRAMMemory):
                    def __init__(self):
                        super().__init__(width, 16)
                        self.read_count = {}

                    def _read(self, address):
                        value = super()._read(address)
                        count = self.read_count.get(address, 0) + 1
                        self.read_count[address] = count
                        if address == base_word + injected_beat and count == 2:
                            return value ^ injected_xor
                        return value

                memory = CorruptSecondRead()

                seed_counter = Signal()

                @passive
                def write_handler():
                    address = 0
                    pending = 0
                    prng = random.Random(42)
                    yield wp.cmd.ready.eq(0)
                    while True:
                        yield wp.wdata.ready.eq(0)
                        if not (yield seed_counter):
                            yield
                        elif pending:
                            while (yield wp.wdata.valid) == 0:
                                yield
                            while prng.randrange(100) < 35:
                                yield
                            yield wp.wdata.ready.eq(1)
                            yield
                            memory._write(address, (yield wp.wdata.data),
                                (yield wp.wdata.we))
                            yield wp.wdata.ready.eq(0)
                            yield
                            pending = 0
                            yield
                        elif (yield wp.cmd.valid):
                            pending = (yield wp.cmd.we)
                            address = (yield wp.cmd.addr)
                            if pending:
                                while prng.randrange(100) < 20:
                                    yield
                                yield wp.cmd.ready.eq(1)
                                yield
                                yield wp.cmd.ready.eq(0)
                        yield

                def start():
                    yield dut._start.re.eq(1)
                    yield
                    yield dut._start.re.eq(0)

                def wait_done():
                    for _ in range(400000):
                        if (yield dut._done.status):
                            return
                        yield
                    self.fail('512-bit repeat benchmark did not finish')

                def main():
                    yield dut.allowed.eq(1)
                    yield dut._base.storage.eq(base)
                    yield dut._length.storage.eq(beats * beat_bytes)
                    yield dut._random.storage.eq(0)
                    yield dut._skip_beats.storage.eq(0)
                    yield dut._repeat_verify.storage.eq(1)
                    yield dut._repeat_idle_cycles.storage.eq(3)
                    yield from start()

                    # Seed the two 31-bit counters after FSM reset, while the
                    # memory endpoint is held off. This covers the same payload
                    # values as a long walk to each boundary without simulating
                    # tens of thousands of otherwise idle skip cycles.
                    for _ in range(20):
                        if (yield in_write):
                            break
                        yield
                    else:
                        self.fail('benchmark did not reach write state')
                    pattern_counter = dut.pattern._submodules[1][1].o
                    expected_counter = dut.expected._submodules[1][1].o
                    yield pattern_counter.eq(first_counter)
                    yield expected_counter.eq(first_counter)
                    yield seed_counter.eq(1)
                    for _ in range(1000):
                        if (yield in_repeat_read):
                            break
                        yield
                    else:
                        self.fail('benchmark did not reach repeat-read state')
                    # The repeat FSM deliberately resets and replays skipped
                    # beats. Seed its expected counter after that reset so the
                    # short simulation lands at the same high counter values.
                    yield expected_counter.eq(first_counter)
                    yield from wait_done()

                    self.assertEqual((yield dut._fault.status), 0)
                    self.assertEqual((yield dut._write_beats.status), beats)
                    self.assertEqual((yield dut._read_beats.status), beats)
                    self.assertEqual((yield dut._repeat_read_beats.status), beats)
                    self.assertEqual((yield dut._errors.status), 0)
                    self.assertEqual((yield dut._repeat_errors.status), 1)
                    self.assertEqual((yield dut._repeat_first_error_offset.status),
                        base + injected_beat * beat_bytes)

                    expected = counter_payload(first_counter + injected_beat)
                    self.assertEqual((yield dut._repeat_first_error_expected.status), expected)
                    self.assertEqual((yield dut._repeat_first_error_actual.status),
                        expected ^ injected_xor)
                    self.assertEqual((yield dut._repeat_first_error_xor.status), injected_xor)

                    for beat in range(beats):
                        stored = memory.mem[(base_word + beat) % memory.depth]
                        self.assertEqual(stored, counter_payload(first_counter + beat))

                simulate(dut, [main(), write_handler(),
                    memory.read_handler(rp, rdata_valid_random=45)])

    def test_reject_and_timeout(self):
        wp = LiteDRAMNativePort('write', 10, 128)
        rp = LiteDRAMNativePort('read', 10, 128)
        dut = NativeDMABenchmark(wp, rp, capacity=16384)
        def start():
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            for _ in range(4):
                yield
        def main():
            yield dut._base.storage.eq(0)
            yield dut._length.storage.eq(16)
            yield from start()
            self.assertEqual((yield dut._fault.status), 2)
            yield dut.allowed.eq(1)
            yield dut._base.storage.eq(16384)
            yield from start()
            self.assertEqual((yield dut._fault.status), 1)
            yield dut._base.storage.eq(1)
            yield from start()
            self.assertEqual((yield dut._fault.status), 1)
            yield dut._base.storage.eq(0xfffffff0)
            yield dut._length.storage.eq(32)
            yield from start()
            self.assertEqual((yield dut._fault.status), 1)
            yield dut._length.storage.eq(16)
            yield dut._base.storage.eq(0)
            yield dut._timeout.storage.eq(16)
            yield from start()
            for _ in range(32):
                yield
            self.assertEqual((yield dut._fault.status), 3)
            self.assertEqual((yield dut._done.status), 1)
            self.assertEqual((yield dut._busy.status), 0)
            yield from start()
            self.assertEqual((yield dut._fault.status), 3)
        simulate(dut, main())

    def test_lost_readiness_is_fatal_and_cannot_restart(self):
        wp = LiteDRAMNativePort('write', 10, 128)
        rp = LiteDRAMNativePort('read', 10, 128)
        dut = NativeDMABenchmark(wp, rp, capacity=16384)
        def main():
            yield dut.allowed.eq(1)
            yield dut._base.storage.eq(0)
            yield dut._length.storage.eq(4096)
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            for _ in range(10):
                yield
            self.assertEqual((yield dut._busy.status), 1)
            yield dut.allowed.eq(0)
            for _ in range(5):
                yield
            self.assertEqual((yield dut._fault.status), 4)
            self.assertEqual((yield dut._done.status), 1)
            self.assertEqual((yield dut._busy.status), 0)
            yield dut.allowed.eq(1)
            yield dut._start.re.eq(1)
            yield
            yield dut._start.re.eq(0)
            for _ in range(5):
                yield
            self.assertEqual((yield dut._fault.status), 4)
            self.assertEqual((yield dut._busy.status), 0)
        simulate(dut, main())

if __name__=='__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DMATest)
    sys.exit(not unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite).wasSuccessful())
