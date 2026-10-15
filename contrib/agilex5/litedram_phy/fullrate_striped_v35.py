"""V35: stable-head dispatch queue with predecoded BG/read-write metadata."""

from functools import reduce
from operator import or_, and_

from migen import *
from migen.fhdl.structure import _Assign
from litex.soc.interconnect import stream
from litedram.common import LiteDRAMNativePort
from litedram_phy.fullrate_elastic_buffer_v3 import FullRateElasticBuffer
from litedram_phy.fullrate_elastic_buffer_v3 import FullRateElasticBuffer as DispatchBuffer


class OutputRegisteredFIFO(Module):
    """Buffered block RAM plus two elastic output registers.

    The final valid register cuts the RAM selector/enable fanout from the
    downstream data mux and return-valid logic. Include both elastic slots in
    level, so drained cannot become true while an output word is in flight.
    """
    def __init__(self, layout, depth, buffered=True):
        assert buffered
        self.submodules.ram = ram = stream.SyncFIFO(layout, depth, buffered=True)
        # Register the inner synchronous block-RAM FIFO's empty/full flags.
        # The count and pointers remain owned by Migen's SyncFIFO; these two
        # flags mirror its level updates cycle-for-cycle without a level
        # comparator driving the RAM enable/read path.
        fifo = ram.fifo.fifo
        removed = {fifo.readable: 0, fifo.writable: 0}
        retained = []
        for statement in fifo._fragment.comb:
            if isinstance(statement, _Assign) and statement.l in removed:
                removed[statement.l] += 1
            else:
                retained.append(statement)
        if removed != {fifo.readable: 1, fifo.writable: 1}:
            raise ValueError("unexpected Migen SyncFIFO readable/writable assignments")
        fifo._fragment.comb = retained
        fifo.readable.reset = Constant(0, 1)
        fifo.writable.reset = Constant(1, 1)
        fifo.readable.attr.add("no_retiming")
        fifo.writable.attr.add("no_retiming")
        # Predict the core RAM's actual writable flag conservatively from its
        # registered occupancy.  With at most one push per cycle, a prior
        # level <= depth-2 guarantees current level <= depth-1 (not full).
        self.admission_allow = admission_allow = Signal(reset=1)
        admission_allow.attr.add("no_retiming")
        self.sync += admission_allow.eq(fifo.level <= depth - 2)
        push = fifo.we & fifo.writable & ~fifo.replace
        pop = fifo.re & fifo.readable
        # Exact next-state flags without mutually-exclusive enable feedback.
        # Simultaneous push/pop leaves occupancy unchanged, including level 1
        # and depth-1; full disallows push, empty disallows pop.
        self.sync += [
            fifo.readable.eq(push | (fifo.readable & ~(pop & (fifo.level == 1)))),
            fifo.writable.eq(pop | (fifo.writable & ~(push & (fifo.level == depth - 1)))),
        ]
        self.submodules.output0 = output0 = stream.Buffer(ram.source.description,
            pipe_valid=True, pipe_ready=False)
        self.submodules.output1 = output1 = FullRateElasticBuffer(ram.source.description)
        output1.source.valid.attr.add("no_retiming")
        self.comb += [ram.source.connect(output0.sink),
            output0.source.connect(output1.sink)]
        self.sink = ram.sink
        self.source = output1.source
        self.nonempty = Signal()
        # Empty is a boolean OR of occupancy flags, not the zero test of two
        # additions used to expose the full occupancy count for diagnostics.
        self.comb += self.nonempty.eq(ram.fifo.fifo.readable | ram.source.valid |
            output0.source.valid | output1.source.valid)
        self.level = Signal(max=depth+5)
        self.comb += self.level.eq(ram.level + output0.source.valid + output1.level)



class FullRateStripedPort(Module):
    """Adapt one 256-bit native port to four independent native ports.

    Requests retain their full address; the receiving crossbar can therefore
    decode BG from address bits 1:0.  Commands and write data are joined in
    order. Read return data is reordered to the parent's read-command order.
    """
    def __init__(self, children, address_width=28):
        if len(children) != 4:
            raise ValueError("FullRateStripedPort requires four child ports")
        if any(p.data_width != 256 or p.address_width != address_width for p in children):
            raise ValueError("child ports must be 256-bit with matching address width")
        self.port = parent = LiteDRAMNativePort("both", address_width, 256)
        self.children = children
        self.error = Signal()       # sticky queue overflow / unsolicited return
        self.drained = Signal()
        # Two elastic slots isolate return ordering from DMA/CPU ready paths.
        self.submodules.return_buffer = return_buffer = stream.Buffer(
            parent.rdata.description, pipe_valid=True, pipe_ready=True)
        self.comb += return_buffer.source.connect(parent.rdata)
        return_endpoint = return_buffer.sink
        return_level = Signal(2)
        return_level.attr.add("no_retiming")
        return_put = return_endpoint.valid & return_endpoint.ready
        return_get = return_buffer.source.valid & return_buffer.source.ready
        self.sync += If(return_put & ~return_get, return_level.eq(return_level + 1)).Elif(
            return_get & ~return_put, return_level.eq(return_level - 1))

        cmd_layout = [("we", 1), ("addr", address_width), ("bg", 2)]
        data_layout = [("data", 256), ("we", 32)]
        parent_cmd = OutputRegisteredFIFO(cmd_layout, 64, buffered=True)
        parent_data = OutputRegisteredFIFO(data_layout, 64, buffered=True)
        self.submodules += parent_cmd, parent_data
        self.admission_fifos = [parent_cmd, parent_data]
        self.comb += [
            parent_cmd.sink.valid.eq(parent.cmd.valid),
            parent_cmd.sink.we.eq(parent.cmd.we),
            parent_cmd.sink.addr.eq(parent.cmd.addr),
            parent_cmd.sink.bg.eq(parent.cmd.addr[:2]),
            parent_data.sink.valid.eq(parent.wdata.valid),
            parent_data.sink.data.eq(parent.wdata.data),
            parent_data.sink.we.eq(parent.wdata.we),
        ]

        cmd_fifos, data_fifos, read_fifos = [], [], []
        pending_writes, reservations = [], []
        reserve_nonzero_flags, reserve_allow32_flags = [], []
        self.pending_state = []
        self.reservation_state = []
        for bg, child in enumerate(children):
            cf = OutputRegisteredFIFO(cmd_layout, 8, buffered=True)
            df = OutputRegisteredFIFO(data_layout, 16, buffered=True)
            rf = OutputRegisteredFIFO([("data", 256)], 32, buffered=True)
            self.submodules += cf, df, rf
            self.admission_fifos += [cf, df, rf]
            cmd_fifos.append(cf); data_fifos.append(df); read_fifos.append(rf)
            pending = Signal(5)
            reserve = Signal(6)
            pending_nonzero = Signal(reset=0)
            pending_allow16 = Signal(reset=1)
            reserve_nonzero = Signal(reset=0)
            reserve_allow32 = Signal(reset=1)
            for flag in (pending_nonzero, pending_allow16, reserve_nonzero, reserve_allow32):
                flag.attr.add("no_retiming")
            pending_writes.append(pending); reservations.append(reserve)
            reserve_nonzero_flags.append(reserve_nonzero)
            reserve_allow32_flags.append(reserve_allow32)
            self.pending_state.append((pending, pending_nonzero, pending_allow16))
            self.reservation_state.append((reserve, reserve_nonzero, reserve_allow32))

            write_cmd_fire = Signal()
            write_data_fire = Signal()
            self.comb += [
                write_cmd_fire.eq(child.cmd.valid & child.cmd.ready & child.cmd.we),
                write_data_fire.eq(child.wdata.valid & child.wdata.ready),
                child.cmd.valid.eq(cf.source.valid & (~cf.source.we | pending_allow16)),
                child.cmd.we.eq(cf.source.we), child.cmd.addr.eq(Cat(Constant(bg,2), cf.source.addr[2:])),
                cf.source.ready.eq(child.cmd.ready & (~cf.source.we | pending_allow16)),
                child.wdata.valid.eq(pending_nonzero & df.source.valid),
                child.wdata.data.eq(df.source.data), child.wdata.we.eq(df.source.we),
                df.source.ready.eq(child.wdata.ready & pending_nonzero),
                rf.sink.valid.eq(child.rdata.valid), rf.sink.data.eq(child.rdata.data),
                child.rdata.ready.eq(1),
            ]
            increment_pending = write_cmd_fire & ~write_data_fire
            decrement_pending = write_data_fire & ~write_cmd_fire
            self.sync += [
                If(child.rdata.valid & ~rf.sink.ready, self.error.eq(1)),
                If(child.rdata.valid & ~reserve_nonzero, self.error.eq(1)),
                If(increment_pending, pending.eq(pending + 1)).Elif(
                    decrement_pending, pending.eq(pending - 1)),
                pending_nonzero.eq(write_cmd_fire | (
                    pending_nonzero & ~(write_data_fire & (pending == 1)))),
                pending_allow16.eq(write_data_fire | (
                    pending_allow16 & ~(write_cmd_fire & (pending == 15)))),
            ]

        # Join independently arriving command and data streams.  Homogeneous
        # read/write phases are used by the DMA and diagnostic masters.
        read_order = OutputRegisteredFIFO([("bg_select", 4)], 64, buffered=True)
        self.submodules += read_order
        self.admission_fifos.append(read_order)
        # Capture command and optional write payload into a two-slot queue.
        # Its input ready depends only on registered fullness, cutting child
        # capacity/selection feedback from both wide parent FIFO head enables.
        self.submodules.dispatch = dispatch = DispatchBuffer(
            cmd_layout + [("read_bg", 4), ("write_bg", 4),
                          ("data", 256), ("byte_enable", 32)])
        enqueue_ready = dispatch.sink.ready & (
            ~parent_cmd.source.we | parent_data.source.valid)
        self.comb += [
            dispatch.sink.valid.eq(parent_cmd.source.valid & (
                ~parent_cmd.source.we | parent_data.source.valid)),
            dispatch.sink.we.eq(parent_cmd.source.we),
            dispatch.sink.addr.eq(parent_cmd.source.addr),
            dispatch.sink.bg.eq(parent_cmd.source.bg),
            # Predecode from the joined command before it enters the dispatch
            # FIFO. These payload tags travel with the command through the
            # read-pointer mux, avoiding BG/we decode on its output path.
            dispatch.sink.read_bg.eq(Mux(parent_cmd.source.we, 0,
                1 << parent_cmd.source.bg)),
            dispatch.sink.write_bg.eq(Mux(parent_cmd.source.we,
                1 << parent_cmd.source.bg, 0)),
            dispatch.sink.data.eq(parent_data.source.data),
            dispatch.sink.byte_enable.eq(parent_data.source.we),
            parent_cmd.source.ready.eq(enqueue_ready),
            parent_data.source.ready.eq(parent_cmd.source.valid &
                parent_cmd.source.we & dispatch.sink.ready),
        ]
        read_bg = dispatch.source.read_bg
        write_bg = dispatch.source.write_bg
        local_admits = []
        local_readies = []
        for i, (cf, df) in enumerate(zip(cmd_fifos, data_fifos)):
            read_selected = read_bg[i]
            write_selected = write_bg[i]
            local_ready = Signal(name=f"dispatch_ready_bg{i}")
            local_admit = Signal(name=f"dispatch_admit_bg{i}")
            local_readies.append(local_ready)
            local_admits.append(local_admit)
            self.comb += [
                local_ready.eq(
                    (read_selected & cf.admission_allow &
                        read_order.admission_allow & reserve_allow32_flags[i]) |
                    (write_selected & cf.admission_allow & df.admission_allow)),
                local_admit.eq(dispatch.source.valid & local_ready),
                cf.sink.valid.eq(local_admit),
                cf.sink.we.eq(dispatch.source.we),
                cf.sink.addr.eq(dispatch.source.addr),
                cf.sink.bg.eq(dispatch.source.bg),
                df.sink.valid.eq(local_admit & write_selected),
                df.sink.data.eq(dispatch.source.data),
                df.sink.we.eq(dispatch.source.byte_enable),
            ]
        read_admit = reduce(or_, [local_admits[i] & read_bg[i] for i in range(4)])
        self.comb += [
            dispatch.source.ready.eq(reduce(or_, local_readies)),
            read_order.sink.valid.eq(read_admit),
            read_order.sink.bg_select.eq(read_bg),
        ]

        # Read data is captured continuously.  The reservation bound guarantees
        # capacity even when the parent stalls and children return out of order.
        selected_return_data = reduce(or_, [Mux(read_order.source.bg_select[i], rf.source.data, 0)
            for i, rf in enumerate(read_fifos)])
        return_valid_terms = []
        self.comb += [
            return_endpoint.data.eq(selected_return_data),
        ]
        consumed_terms = []
        for i, rf in enumerate(read_fifos):
            selected = read_order.source.bg_select[i]
            self.comb += rf.source.ready.eq(read_order.source.valid & selected & return_endpoint.ready)
            consumed = rf.source.valid & rf.source.ready
            admitted = local_admits[i] & read_bg[i]
            return_valid_terms.append(read_order.source.valid & selected & rf.source.valid)
            consumed_terms.append(consumed)
            increment_reservation = admitted & ~consumed
            decrement_reservation = consumed & ~admitted
            self.sync += [
                If(increment_reservation, reservations[i].eq(reservations[i] + 1)).Elif(
                    decrement_reservation, reservations[i].eq(reservations[i] - 1)),
                reserve_nonzero_flags[i].eq(admitted | (
                    reserve_nonzero_flags[i] &
                    ~(consumed & (reservations[i] == 1)))),
                reserve_allow32_flags[i].eq(consumed | (
                    reserve_allow32_flags[i] &
                    ~(admitted & (reservations[i] == 31)))),
            ]
        self.comb += [
            return_endpoint.valid.eq(reduce(or_, return_valid_terms)),
            read_order.source.ready.eq(reduce(or_, consumed_terms)),
        ]

        self.comb += [
            parent.cmd.ready.eq(parent_cmd.sink.ready),
            parent.wdata.ready.eq(parent_data.sink.ready),
        ]
        # Register local emptiness, then combine registered groups.  Drain is
        # deliberately delayed; DMA waits 32 drain cycles after all commands
        # and transfers finish, so a transient stale idle cannot finish a run.
        parent_idle = Signal(reset=1)
        child_idle = [Signal(reset=1) for _ in range(4)]
        for sig in [parent_idle] + child_idle + [self.drained]:
            sig.attr.add("no_retiming")
        self.sync += parent_idle.eq(~parent_cmd.nonempty & ~parent_data.nonempty & ~read_order.nonempty & ~dispatch.nonempty & (return_level == 0))
        for sig, cf, df, rf, pending in zip(child_idle, cmd_fifos, data_fifos, read_fifos, pending_writes):
            self.sync += sig.eq(~cf.nonempty & ~df.nonempty & ~rf.nonempty & (pending == 0))
        self.sync += self.drained.eq(parent_idle & reduce(and_, child_idle))



# Short name used by the SoC generator.
StripedPort = FullRateStripedPort
