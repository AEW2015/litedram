#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Native-width sequential DMA bandwidth and integrity test, in the sys domain.

Writes and reads are separate phases: their bandwidths must not be added.
Write timing includes DMA FIFO drain to the native port; native writes have
no completion response. A 128-cycle settling interval precedes verification.
Timeout/lost-ready faults require FPGA reconfiguration (outstanding traffic
must not be discarded by resetting just the benchmark).
"""

from math import ceil
from operator import or_
from functools import reduce

from migen import *

from litex.gen import LiteXModule
from litex.soc.interconnect.csr import CSR, CSRStorage, CSRStatus

from litedram.frontend.dma import LiteDRAMDMAReader, LiteDRAMDMAWriter
from litedram.frontend.bist import Generator
from litedram.frontend.native_benchmark_compare import MismatchRegisters


# Native DMA benchmark ----------------------------------------------------------------------------

class NativeDMABenchmark(LiteXModule):
    def __init__(self, write_port, read_port, capacity=0x40000000, fifo_depth=64, drained=None,
                 databits=32, capture_first_error=False, external_start=None):
        assert write_port.data_width == read_port.data_width
        assert write_port.clock_domain == read_port.clock_domain == 'sys'
        width                    = write_port.data_width
        beat_bytes               = width//8
        shift                    = log2_int(beat_bytes)
        self.allowed             = Signal()
        self.external_start      = Signal() if external_start is None else external_start
        # Diagnostic pulse at the first accepted request of each repeated read.
        self.repeat_read_start   = Signal()
        self._start              = CSR()
        self._base               = CSRStorage(32, reset=0x01000000)  # offset from SDRAM origin
        self._length             = CSRStorage(32, reset=0x04000000)
        self._random             = CSRStorage(reset=1)  # PRBS31 or increasing counter
        self._read_only          = CSRStorage()
        self._skip_beats         = CSRStorage(32, reset=0)
        # Optional second read pass for isolating transient read failures.
        self._repeat_verify      = CSRStorage()
        self._repeat_idle_cycles = CSRStorage(32, reset=0)
        self._repeat_read_cycles = CSRStatus(32)
        self._repeat_read_beats  = CSRStatus(32)
        self._repeat_read_stalls = CSRStatus(32)
        self._repeat_errors      = CSRStatus(32)
        self._repeat_first_error_offset = CSRStatus(32)
        self._repeat_first_error_xor = CSRStatus(width)
        self._timeout            = CSRStorage(32, reset=1_000_000_000)
        self._busy               = CSRStatus()
        self._done               = CSRStatus()
        self._fault              = CSRStatus(3)  # 1=config, 2=not trained, 3=timeout, 4=lost ready
        self._write_cycles       = CSRStatus(32)
        self._read_cycles        = CSRStatus(32)
        self._write_beats        = CSRStatus(32)
        self._read_beats         = CSRStatus(32)
        self._write_stalls       = CSRStatus(32)
        self._read_stalls        = CSRStatus(32)
        self._errors             = CSRStatus(32)
        self._first_error_offset = CSRStatus(32)
        self._first_error_xor    = CSRStatus(width)
        if capture_first_error:
            self._first_error_actual   = CSRStatus(width)
            self._first_error_expected = CSRStatus(width)
            self._repeat_first_error_actual = CSRStatus(width)
            self._repeat_first_error_expected = CSRStatus(width)
        self._data_width         = CSRStatus(16, reset=width)
        self._fifo_depth         = CSRStatus(16, reset=fifo_depth)
        self._dq_error_mask      = CSRStatus(databits)
        self.writer = writer = LiteDRAMDMAWriter(write_port, fifo_depth, fifo_buffered=True)
        self.reader = reader = LiteDRAMDMAReader(read_port, fifo_depth, fifo_buffered=True)
        self.pattern = pattern = ResetInserter()(Generator(31, 31, [27, 30]))
        self.expected = expected = ResetInserter()(Generator(31, 31, [27, 30]))
        base         = Signal(32)
        beat_count   = Signal(32)
        skip_remaining = Signal(32)
        skip_cycles = Signal(32)
        last_beat    = Signal(32, reset=0xffffffff)
        random_data  = Signal()
        read_only    = Signal()
        timeout      = Signal(32)
        write_index  = Signal(32)
        read_index   = Signal(32)
        received     = Signal(32)
        write_cycles = Signal(32)
        read_cycles  = Signal(32)
        write_beats  = Signal(32)
        read_beats   = Signal(32)
        write_stalls = Signal(32)
        read_stalls  = Signal(32)
        repeat_read_stalls = Signal(32)
        errors       = Signal(32)
        repeat_read_cycles = Signal(32)
        repeat_read_beats = Signal(32)
        repeat_errors = Signal(32)
        repeat_first_offset = Signal(32)
        repeat_first_xor = Signal(width)
        repeat_idle_remaining = Signal(32)
        skip_total = Signal(32)
        repeat_skip_remaining = Signal(32)
        repeat_skip_cycles = Signal(32)
        first_offset = Signal(32)
        first_xor    = Signal(width)
        dq_errors    = Signal(databits)
        self.comb += self._dq_error_mask.status.eq(dq_errors)
        fault           = Signal(3)
        settle          = Signal(8)
        mismatch        = Signal()
        mismatch_offset = Signal(32)
        mismatch_xor    = Signal(width)
        expected_data   = Signal(width)
        skip_advance    = Signal()
        captured_actual = Signal(width) if capture_first_error else None
        captured_expected = Signal(width) if capture_first_error else None
        repeat_captured_actual = Signal(width) if capture_first_error else None
        repeat_captured_expected = Signal(width) if capture_first_error else None
        # Register the wide comparison before updating counters/error metadata.
        check_valid = Signal()
        self.mismatch_registers = compare = MismatchRegisters(width)
        self.comb += [compare.a.eq(reader.source.data),
            expected_data.eq(Replicate(expected.o, ceil(width/31))[:width]),
            compare.b.eq(expected_data),
            compare.enable.eq(reader.source.valid & reader.source.ready), mismatch.eq(compare.o)]
        capture_payload = []
        if capture_first_error:
            capture_payload = [
                captured_actual.eq(reader.source.data),
                captured_expected.eq(expected_data),
            ]
            repeat_capture_payload = [
                repeat_captured_actual.eq(reader.source.data),
                repeat_captured_expected.eq(expected_data),
            ]
        else:
            repeat_capture_payload = []
        self.sync += [check_valid.eq(reader.source.valid & reader.source.ready),
            If(reader.source.valid & reader.source.ready,
                mismatch_xor.eq(reader.source.data ^ expected_data),
                mismatch_offset.eq(base+(received<<shift)),
                *capture_payload, *repeat_capture_payload)]
        self.comb += [pattern.random_enable.eq(random_data), expected.random_enable.eq(random_data),
            writer.sink.address.eq((base>>shift)+write_index),
            writer.sink.data.eq(Replicate(pattern.o, ceil(width/31))[:width]),
            writer.sink.last.eq(write_index==last_beat),
            reader.sink.address.eq((base>>shift)+read_index),
            reader.sink.last.eq(read_index==last_beat),
            pattern.ce.eq(skip_advance | (writer.sink.valid & writer.sink.ready)),
            expected.ce.eq(skip_advance | (reader.source.valid & reader.source.ready)),
            self._fault.status.eq(fault), self._write_cycles.status.eq(write_cycles),
            self._read_cycles.status.eq(read_cycles), self._write_beats.status.eq(write_beats),
            self._read_beats.status.eq(read_beats), self._write_stalls.status.eq(write_stalls),
            self._read_stalls.status.eq(read_stalls), self._errors.status.eq(errors),
            self._repeat_read_cycles.status.eq(repeat_read_cycles),
            self._repeat_read_beats.status.eq(repeat_read_beats),
            self._repeat_read_stalls.status.eq(repeat_read_stalls),
            self._repeat_errors.status.eq(repeat_errors),
            self._repeat_first_error_offset.status.eq(repeat_first_offset),
            self._repeat_first_error_xor.status.eq(repeat_first_xor),
            self._first_error_offset.status.eq(first_offset), self._first_error_xor.status.eq(first_xor)]
        end   = Signal(33)
        valid = Signal()
        self.comb += [end.eq(self._base.storage+self._length.storage),
            valid.eq((self._length.storage!=0) & (end<=capacity) &
                ((self._base.storage & (beat_bytes-1))==0) &
                ((self._length.storage & (beat_bytes-1))==0) & (self._timeout.storage!=0))]
        self.fsm = fsm = FSM(reset_state='IDLE')
        capture_reset = []
        if capture_first_error:
            capture_reset = [
                NextValue(self._first_error_actual.status, 0),
                NextValue(self._first_error_expected.status, 0),
                NextValue(self._repeat_first_error_actual.status, 0),
                NextValue(self._repeat_first_error_expected.status, 0),
            ]
        idle = [If(self._start.re | self.external_start,
            NextValue(write_cycles, 0), NextValue(read_cycles, 0), NextValue(write_beats, 0),
            NextValue(read_beats, 0), NextValue(write_stalls, 0), NextValue(read_stalls, 0),
            NextValue(skip_cycles, 0),
            NextValue(errors, 0), NextValue(first_offset, 0), NextValue(first_xor, 0), NextValue(dq_errors, 0),
            NextValue(repeat_read_cycles, 0), NextValue(repeat_read_beats, 0),
            NextValue(repeat_read_stalls, 0),
            NextValue(repeat_errors, 0), NextValue(repeat_first_offset, 0), NextValue(repeat_first_xor, 0),
            NextValue(repeat_idle_remaining, 0),
            NextValue(repeat_skip_cycles, 0),
            *capture_reset,
            NextValue(write_index, 0), NextValue(read_index, 0), NextValue(received, 0), NextValue(fault, 0),
            If(~valid, NextValue(fault, 1), NextState('REJECTED'))
            .Elif(~self.allowed, NextValue(fault, 2), NextState('REJECTED'))
            .Else(NextValue(base, self._base.storage), NextValue(beat_count, self._length.storage>>shift),
                NextValue(last_beat, (self._length.storage>>shift)-1),
                NextValue(skip_remaining, self._skip_beats.storage),
                NextValue(skip_total, self._skip_beats.storage),
                NextValue(random_data, self._random.storage), NextValue(read_only, self._read_only.storage),
                NextValue(timeout, self._timeout.storage), NextState('RESET')))]
        fsm.act('IDLE', *idle)
        fsm.act('REJECTED', self._done.status.eq(1), *idle)
        fsm.act('DONE', self._done.status.eq(1), *idle)
        fsm.act('RESET', pattern.reset.eq(1), expected.reset.eq(1),
            If(skip_remaining != 0, NextState('SKIP'))
            .Else(If(read_only, NextState('READ')).Else(NextState('WRITE'))))
        fsm.act('SKIP',
            If(skip_cycles >= timeout, NextValue(fault, 3), NextState('FAULT'))
            .Elif(~self.allowed, NextValue(fault, 4), NextState('FAULT'))
            .Elif(skip_remaining == 0,
                If(read_only, NextState('READ')).Else(NextState('WRITE')))
            .Else(skip_advance.eq(1), NextValue(skip_cycles, skip_cycles+1),
                If(skip_remaining == 1,
                    NextValue(skip_remaining, 0),
                    If(read_only, NextState('READ')).Else(NextState('WRITE')))
                .Else(NextValue(skip_remaining, skip_remaining-1))))
        fsm.act('WRITE', writer.sink.valid.eq(write_index<beat_count),
            NextValue(write_cycles, write_cycles+1),
            If(writer.sink.valid & writer.sink.ready, NextValue(write_index, write_index+1)),
            If(write_port.cmd.valid & ~write_port.cmd.ready, NextValue(write_stalls, write_stalls+1)),
            If(write_port.wdata.valid & write_port.wdata.ready,
                NextValue(write_beats, write_beats+1),
                If(write_beats==last_beat, NextValue(settle, 127), NextState('SETTLE' if drained is None else 'DRAIN'))),
            If(write_cycles>=timeout, NextValue(fault, 3), NextState('FAULT')),
            If(~self.allowed, NextValue(fault, 4), NextState('FAULT')))
        fsm.act('DRAIN', NextValue(write_cycles, write_cycles+1),
            If(1 if drained is None else drained, NextState('SETTLE')),
            If(write_cycles>=timeout, NextValue(fault, 3), NextState('FAULT')),
            If(~self.allowed, NextValue(fault, 4), NextState('FAULT')))
        fsm.act('SETTLE', If(settle==0, NextState('READ')).Else(NextValue(settle, settle-1)),
            If(~self.allowed, NextValue(fault, 4), NextState('FAULT')))
        fsm.act('READ', reader.sink.valid.eq(read_index<beat_count), reader.source.ready.eq(1),
            NextValue(read_cycles, read_cycles+1),
            If(reader.sink.valid & reader.sink.ready, NextValue(read_index, read_index+1)),
            If(read_port.cmd.valid & ~read_port.cmd.ready, NextValue(read_stalls, read_stalls+1)),
            If(read_port.rdata.valid & read_port.rdata.ready, NextValue(read_beats, read_beats+1)),
            If(reader.source.valid, NextValue(received, received+1)),
            If(check_valid,
                If(mismatch, NextValue(errors, errors+1),
                    NextValue(dq_errors, dq_errors | reduce(or_, (mismatch_xor[i:i+databits] for i in range(0, width, databits)))),
                    If(errors==0, NextValue(first_offset, mismatch_offset), NextValue(first_xor, mismatch_xor),
                        *([NextValue(self._first_error_actual.status, captured_actual),
                           NextValue(self._first_error_expected.status, captured_expected)]
                          if capture_first_error else []))),
                If(received==beat_count,
                    If(self._repeat_verify.storage,
                        NextValue(repeat_idle_remaining, self._repeat_idle_cycles.storage),
                        NextState('REPEAT_DWELL'))
                    .Else(NextState('DONE')))),
            If(read_cycles>=timeout, NextValue(fault, 3), NextState('FAULT')),
            If(~self.allowed, NextValue(fault, 4), NextState('FAULT')))
        # A second read is part of the same command, with independent results.
        # The controller remains free to refresh during the idle dwell.
        fsm.act('REPEAT_DWELL',
            If(repeat_idle_remaining == 0, NextState('REPEAT_RESET'))
            .Else(NextValue(repeat_idle_remaining, repeat_idle_remaining-1)),
            If(~self.allowed, NextValue(fault, 4), NextState('FAULT')))
        fsm.act('REPEAT_RESET', expected.reset.eq(1),
            NextValue(read_index, 0), NextValue(received, 0),
            NextValue(repeat_skip_remaining, skip_total), NextState('REPEAT_SKIP'))
        fsm.act('REPEAT_SKIP',
            If(repeat_skip_cycles >= timeout, NextValue(fault, 3), NextState('FAULT'))
            .Elif(~self.allowed, NextValue(fault, 4), NextState('FAULT'))
            .Elif(repeat_skip_remaining == 0, NextState('REPEAT_READ'))
            .Else(skip_advance.eq(1), NextValue(repeat_skip_cycles, repeat_skip_cycles+1),
                If(repeat_skip_remaining == 1,
                    NextValue(repeat_skip_remaining, 0), NextState('REPEAT_READ'))
                .Else(NextValue(repeat_skip_remaining, repeat_skip_remaining-1))))
        fsm.act('REPEAT_READ', reader.sink.valid.eq(read_index<beat_count), reader.source.ready.eq(1),
            NextValue(repeat_read_cycles, repeat_read_cycles+1),
            If(reader.sink.valid & reader.sink.ready, NextValue(read_index, read_index+1)),
            If(read_port.cmd.valid & ~read_port.cmd.ready,
                NextValue(repeat_read_stalls, repeat_read_stalls+1)),
            If(read_port.rdata.valid & read_port.rdata.ready, NextValue(repeat_read_beats, repeat_read_beats+1)),
            If(reader.source.valid, NextValue(received, received+1)),
            If(check_valid,
                If(mismatch,
                    NextValue(repeat_errors, repeat_errors+1),
                    If(repeat_errors==0,
                        NextValue(repeat_first_offset, mismatch_offset),
                        NextValue(repeat_first_xor, mismatch_xor),
                        *([NextValue(self._repeat_first_error_actual.status, repeat_captured_actual),
                           NextValue(self._repeat_first_error_expected.status, repeat_captured_expected)]
                          if capture_first_error else []))),
                If(received==beat_count, NextState('DONE'))),
            If(repeat_read_cycles>=timeout, NextValue(fault, 3), NextState('FAULT')),
            If(~self.allowed, NextValue(fault, 4), NextState('FAULT')))
        # Drain already-returned data but never accept another start after a fault.
        fsm.act('FAULT', self._done.status.eq(1), reader.source.ready.eq(1))
        self.comb += self._busy.status.eq(fsm.ongoing('RESET')|fsm.ongoing('WRITE')|
            fsm.ongoing('DRAIN')|fsm.ongoing('SETTLE')|fsm.ongoing('READ')|
            fsm.ongoing('REPEAT_DWELL')|fsm.ongoing('REPEAT_RESET')|
            fsm.ongoing('REPEAT_SKIP')|fsm.ongoing('REPEAT_READ'))
        self.comb += self.repeat_read_start.eq(
            fsm.ongoing('REPEAT_READ') & (read_index == 0) &
            reader.sink.valid & reader.sink.ready)
