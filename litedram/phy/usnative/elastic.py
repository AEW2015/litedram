#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Fabric-side lane buffering for ordered, full-word native read assembly."""

from operator import and_, or_
from functools import reduce
from migen import Cat, If, Module, ResetInserter, Signal
from migen.genlib.fifo import SyncFIFOBuffered


class NativeReadAssembler(Module):
    """Join independent, ordered byte-lane streams with bounded buffering.

    Each accepted lane item is eight DDR samples of one byte (64 bits).
    Input items must already have native FIFO read latency and bitslip applied.
    ``lane_ready`` is a fabric handshake, NOT permission to read a native FIFO:
    an adapter must reserve capacity for every outstanding native read first.
    All lanes must share a transaction sequence. Reset/flush must also cancel
    outstanding native reads; late responses must not enter the next epoch.
    This does not provide the fixed-latency DFI read-valid contract by itself.
    """
    def __init__(self, lanes, *, depth=4):
        if type(lanes) is not int or lanes not in (2, 4, 8):
            raise ValueError('Expected 2, 4 or 8 byte lanes')
        if type(depth) is not int or depth < 2:
            raise ValueError('At least two FIFO entries required')
        self.flush = Signal()
        self.lane_valid = Signal(lanes)
        self.lane_ready = Signal(lanes)
        self.lane_data = [Signal(64) for _ in range(lanes)]
        self.valid = Signal()
        self.ready = Signal()
        self.data = Signal(lanes*64)
        self.level = []
        fifos = []
        for lane in range(lanes):
            fifo = ResetInserter()(SyncFIFOBuffered(64, depth))
            self.submodules += fifo
            fifos.append(fifo)
            self.level.append(fifo.level)
            self.comb += [
                fifo.reset.eq(self.flush), fifo.din.eq(self.lane_data[lane]),
                self.lane_ready[lane].eq(fifo.writable & ~self.flush),
                fifo.we.eq(self.lane_valid[lane] & self.lane_ready[lane]),
                fifo.re.eq(self.valid & self.ready),
            ]
        # All lane FIFOs advance together when the assembled word is accepted,
        # preserving byte-lane correspondence under downstream backpressure.
        self.comb += [
            self.valid.eq(reduce(and_, (fifo.readable for fifo in fifos)) & ~self.flush),
            self.data.eq(Cat(*(fifo.dout for fifo in fifos))),
        ]


class NativeFIFOReceiveAdapter(Module):
    """Buffer independently available native byte lanes before assembly.

    ``lane_empty`` and ``lane_data`` describe one already-combined 64-bit item
    per byte lane. ``response_latency`` is the exact number of fabric clock
    edges from asserting ``read_enable`` to the corresponding ``lane_data``
    being valid. The adapter reserves an assembler slot at issue time, so a
    response can never overrun a full lane queue. The native FIFO itself must
    not have a separate hidden outstanding-read limit smaller than this
    adapter's outstanding pipeline.

    Flush stops new reads, discards all in-flight responses, and holds the
    assembler reset until the response pipeline is empty. The caller must
    still cancel/reset the memory transaction stream and qualify the fixed
    read-latency contract before connecting ``data`` to DFI.
    """
    def __init__(self, layout, *, depth=4, response_latency=1):
        lanes = len(layout.lanes)
        if type(response_latency) is not int or response_latency < 1:
            raise ValueError('Native FIFO response latency must be a positive integer')
        if type(depth) is not int or depth < response_latency + 1:
            raise ValueError('Lane queue depth must exceed the native response latency')

        self.lane_empty = Signal(lanes, reset=(1 << lanes) - 1)
        self.lane_data = [Signal(64) for _ in range(lanes)]
        self.flush = Signal()
        self.ready = Signal()
        self.read_enable = Signal(len(layout.slices))
        self.valid = Signal()
        self.data = Signal(lanes*64)
        self.lane_level = []
        self.in_flight = []

        self.submodules.assembler = assembler = NativeReadAssembler(lanes, depth=depth)
        flush_active = Signal(reset=0)
        issue = Signal(lanes)
        response_pipes = [[Signal(name=f'lane_{lane}_read_valid_{stage}')
                           for stage in range(response_latency)] for lane in range(lanes)]
        response_valid = Signal(lanes)

        # A flush pulse may be shorter than the native return latency. Keep
        # dropping responses and resetting the assembly queues until every
        # reservation made before that pulse has returned.
        outstanding_any = Signal()
        self.comb += outstanding_any.eq(reduce(or_, [reduce(or_, pipe) for pipe in response_pipes]))
        self.sync += If(self.flush, flush_active.eq(1)).Elif(~outstanding_any, flush_active.eq(0))
        active_flush = self.flush | flush_active
        self.comb += [assembler.flush.eq(active_flush), assembler.ready.eq(self.ready),
                      self.valid.eq(assembler.valid), self.data.eq(assembler.data)]

        for lane in layout.lanes:
            index = lane.index
            # Count all issued reads whose data has not yet entered the lane
            # queue. Occupancy + reservations is the credit invariant.
            reservations = Signal(max=depth + 1, name=f'lane_{index}_reserved')
            pipe = response_pipes[index]
            in_flight = Cat(*pipe)
            queue_level = assembler.level[index]
            can_reserve = (queue_level + reservations) < depth
            available = ~self.lane_empty[index]
            self.comb += issue[index].eq(available & can_reserve & ~active_flush)

            self.sync += pipe[0].eq(issue[index])
            for stage in range(1, response_latency):
                self.sync += pipe[stage].eq(pipe[stage-1])
            self.comb += [response_valid[index].eq(pipe[-1] & ~active_flush),
                          assembler.lane_valid[index].eq(response_valid[index]),
                          assembler.lane_data[index].eq(self.lane_data[index])]
            # reservations tracks issue-to-queue-write occupancy, including
            # the response visible this cycle. A flush drains the pipe before
            # dropping the residual accounting state.
            self.sync += If(active_flush & ~in_flight, reservations.eq(0)).Else(
                If(issue[index] & ~response_valid[index], reservations.eq(reservations + 1))
                .Elif(response_valid[index] & ~issue[index], reservations.eq(reservations - 1)))
            self.lane_level.append(queue_level)
            self.in_flight.append(in_flight)
            for tap in lane.dq + (lane.strobe,) + (() if lane.mask is None else (lane.mask,)):
                self.comb += self.read_enable[tap].eq(issue[index])
        for tap in set(range(len(layout.slices))) - {
                tap for lane in layout.lanes
                for tap in lane.dq + (lane.strobe,) + (() if lane.mask is None else (lane.mask,))}:
            self.comb += self.read_enable[tap].eq(0)
