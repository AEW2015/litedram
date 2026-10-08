#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause
"""Owner and data routing for two fixed DDR4 bank-group slots.

Slot 0/1 are fixed physical CAS slots. Each slot carries one native BL8 beat;
payload width follows the child ports (256 bits for x32 and 512 bits for x64).
The module keeps requester identity with each
accepted command, captures write beats at command acceptance, and routes
fixed-latency read returns through per-master queues with credits reserved
before read acceptance.

This is an integration boundary, not a replacement for the crossbar's bank
arbiters.  Its caller gates bank candidates with the per-master ready vectors,
then pulses ``slot_valid`` only for a CAS that was actually accepted.
"""

from functools import reduce
from operator import or_

from migen import *
from litex.soc.interconnect import stream


class DualSlotData(Module):
    """Route owner-tagged CAS payloads between native ports and two slots.

    ``masters`` must be equal-width sys-domain native ports. Before accepting a
    bank command, the caller uses ``master_write_ready`` or
    ``master_read_ready`` to qualify its arbiter candidate. ``slot_valid`` is
    the accepted-CAS event, not a candidate valid; its owner, direction and
    payload are captured on that event. A write data head and mask are
    captured and presented on the corresponding slot after ``write_latency``
    cycles. Reads reserve one response credit at command acceptance, then the
    corresponding fixed-latency slot return is buffered until the master's
    ``rdata.ready`` accepts it.

    The upstream interleaved crossbar should prevent a master from owning both
    bank groups at once.  A same-owner dual grant is rejected to avoid
    consuming one native data beat twice or requiring hidden reassembly.
    """
    def __init__(self, masters, *, read_latency, write_latency, depth=32):
        if len(masters) < 1 or depth < 1 or read_latency < 1 or write_latency < 1:
            raise ValueError("invalid master count, queue depth, or latency")
        data_width = masters[0].data_width
        if any(m.data_width != data_width or data_width % 8 or m.clock_domain != "sys"
               for m in masters):
            raise ValueError("DualSlotData requires equal-width byte-aligned sys-domain native ports")
        mask_width = data_width // 8
        self.masters = masters
        n = len(masters)
        ownerbits = max(1, log2_int(n, False))
        self.slot_valid = [Signal(name=f"slot{i}_valid") for i in range(2)]
        self.slot_write = [Signal(name=f"slot{i}_write") for i in range(2)]
        self.slot_owner = [Signal(ownerbits, name=f"slot{i}_owner") for i in range(2)]
        self.slot_ready = [Signal(name=f"slot{i}_ready") for i in range(2)]
        self.slot_wdata = [Signal(data_width, name=f"slot{i}_wdata") for i in range(2)]
        self.slot_wdata_we = [Signal(mask_width, name=f"slot{i}_wdata_we") for i in range(2)]
        self.slot_wdata_valid = [Signal(name=f"slot{i}_wdata_valid") for i in range(2)]
        self.slot_rdata = [Signal(data_width, name=f"slot{i}_rdata") for i in range(2)]
        # Use these before command arbitration. slot_ready is the corresponding
        # owner-selected view for each fixed slot; slot_valid remains an
        # accepted-CAS event from the caller.
        self.master_read_ready = [Signal(name=f"master{i}_read_ready") for i in range(n)]
        self.master_write_ready = [Signal(name=f"master{i}_write_ready") for i in range(n)]
        self.error = Signal()

        # A duplicate owner cannot supply two independent native beats.
        duplicate_pair = (self.slot_valid[0] & self.slot_valid[1] &
                          (self.slot_owner[0] == self.slot_owner[1]))
        self.sync += If(duplicate_pair, self.error.eq(1))

        credits = [Signal(max=depth + 1, name=f"read_credit{i}") for i in range(n)]
        queues = []
        for mi, master in enumerate(masters):
            queue = stream.SyncFIFO([("data", data_width)], depth, buffered=True)
            setattr(self.submodules, f"read_queue{mi}", queue)
            queues.append(queue)
            self.comb += [
                master.rdata.valid.eq(queue.source.valid),
                master.rdata.data.eq(queue.source.data),
                queue.source.ready.eq(master.rdata.ready),
                self.master_read_ready[mi].eq(credits[mi] < depth),
                self.master_write_ready[mi].eq(master.wdata.valid),
            ]

        # Derive admission from total reserved responses (queued plus in flight).
        for slot in range(2):
            owner = self.slot_owner[slot]
            owner_cases = {}
            for mi, master in enumerate(masters):
                owner_cases[mi] = If(self.slot_write[slot],
                    self.slot_ready[slot].eq(self.master_write_ready[mi])
                ).Else(self.slot_ready[slot].eq(self.master_read_ready[mi]))
            owner_cases["default"] = self.slot_ready[slot].eq(0)
            self.comb += Case(owner, owner_cases)
        self.comb += If(duplicate_pair,
            self.slot_ready[0].eq(0), self.slot_ready[1].eq(0))

        # slot_valid is an accepted CAS event. Admission uses the master ready
        # vectors above, before the bank arbiter asserts this event.
        accepted = self.slot_valid

        # Capture per-slot write payload at the CAS handshake and delay it to
        # the controller data launch point.  Separate pipelines retain masks.
        for slot in range(2):
            dpipe = [Signal(data_width, name=f"slot{slot}_wdata_d{i}")
                     for i in range(write_latency)]
            mpipe = [Signal(mask_width, name=f"slot{slot}_wmask_d{i}")
                     for i in range(write_latency)]
            vpipe = [Signal(name=f"slot{slot}_wvalid_d{i}")
                     for i in range(write_latency)]
            self.comb += [
                self.slot_wdata[slot].eq(dpipe[-1]),
                self.slot_wdata_we[slot].eq(mpipe[-1]),
                self.slot_wdata_valid[slot].eq(vpipe[-1]),
            ]
            first_owner = self.slot_owner[slot]
            first_data = Array([m.wdata.data for m in masters])[first_owner]
            first_mask = Array([m.wdata.we for m in masters])[first_owner]
            self.sync += If(accepted[slot] & self.slot_write[slot],
                dpipe[0].eq(first_data), mpipe[0].eq(first_mask), vpipe[0].eq(1)
            ).Else(vpipe[0].eq(0))
            for stage in range(1, write_latency):
                self.sync += [dpipe[stage].eq(dpipe[stage-1]),
                              mpipe[stage].eq(mpipe[stage-1]),
                              vpipe[stage].eq(vpipe[stage-1])]

        # Native write beats are consumed atomically with their CAS.  Pair
        # exclusion guarantees one ready path per master in any cycle.
        for mi, master in enumerate(masters):
            ready_terms = [accepted[s] & self.slot_write[s] &
                           (self.slot_owner[s] == mi) for s in range(2)]
            self.comb += master.wdata.ready.eq(reduce(or_, ready_terms) if ready_terms else 0)

        # Shift one owner and valid bit for every accepted read slot.
        owner_pipe = [[Signal(ownerbits, name=f"slot{s}_read_owner_d{i}")
                       for i in range(read_latency)] for s in range(2)]
        valid_pipe = [[Signal(name=f"slot{s}_read_valid_d{i}")
                       for i in range(read_latency)] for s in range(2)]
        for slot in range(2):
            self.sync += [
                owner_pipe[slot][0].eq(self.slot_owner[slot]),
                valid_pipe[slot][0].eq(accepted[slot] & ~self.slot_write[slot]),
            ]
            for stage in range(1, read_latency):
                self.sync += [owner_pipe[slot][stage].eq(owner_pipe[slot][stage-1]),
                              valid_pipe[slot][stage].eq(valid_pipe[slot][stage-1])]

        for mi in range(n):
            reads = [accepted[s] & ~self.slot_write[s] & (self.slot_owner[s] == mi)
                     for s in range(2)]
            returns = [valid_pipe[s][-1] &
                       (owner_pipe[s][-1] == mi) for s in range(2)]
            read_count = Signal(2)
            return_count = Signal(2)
            self.comb += [
                read_count.eq(sum(reads)), return_count.eq(sum(returns)),
                queues[mi].sink.valid.eq(returns[0] | returns[1]),
                queues[mi].sink.data.eq(Mux(returns[0], self.slot_rdata[0], self.slot_rdata[1])),
            ]
            # Native crossbar locking excludes two same-owner slots. Treat a
            # violation as fatal because a one-write-port FIFO cannot enqueue
            # both physical returns in the same cycle.
            self.sync += If((return_count > 1) | (read_count > 1), self.error.eq(1))
            self.sync += If(queues[mi].sink.valid & ~queues[mi].sink.ready,
                self.error.eq(1))
            self.sync += credits[mi].eq(credits[mi] + sum(reads) -
                (queues[mi].source.valid & masters[mi].rdata.ready))

        # Return valid is generated from the accepted read's fixed-latency
        # tag.  The caller supplies the two physical data halves on that due
        # cycle; there is no downstream ready signal to the PHY.
