"""V46: DMA45 with issue cursor, spacing, and issued-event factoring."""
from migen import *
from litex.soc.interconnect import stream
from litedram.common import LiteDRAMNativePort
from litedram.frontend.dma import LiteDRAMDMAWriter
from litedram_phy.fullrate_dma_reader_v2 import FullRateDMAReader
from litedram_phy.fullrate_immediate_counter_v1 import ImmediateEventCounter

class MemoryDMA(Module):
    def __init__(self, address_width=28, fifo_depth=32):
        self.port=p=LiteDRAMNativePort('both',address_width,256)
        rp=LiteDRAMNativePort('read',address_width,256)
        wp=LiteDRAMNativePort('write',address_width,256)
        self.submodules.reader=r=FullRateDMAReader(rp,fifo_depth=fifo_depth,fifo_buffered=True)
        self.submodules.writer=w=LiteDRAMDMAWriter(wp,fifo_depth=fifo_depth,fifo_buffered=True)
        self.submodules.write_output=write_output=stream.Buffer(wp.wdata.description,
            pipe_valid=True,pipe_ready=False)
        self.comb += wp.wdata.connect(write_output.sink)
        self.drained=Signal(reset=1)
        self.busy=busy=Signal(); self.done=done=Signal(); self.start=start=Signal()
        self.command_select = command_select = Signal()
        self.write_select = write_select = Signal()
        self.read_select = read_select = Signal()
        for select in (command_select, write_select, read_select):
            select.attr.add("preserve_syn_only")
        # Capture the identical busy transition on the existing edge. The
        # replicas drive separate native-port arbitration cones without an
        # extra transaction cycle or a different timeout/reset condition.

        self.base=base=Signal(address_width); self.length=length=Signal(28)
        self.mode=mode=Signal(); self.seed=seed=Signal(32); self.gap=gap=Signal(16)
        self.submodules.cycles_counter = cycles_counter = ImmediateEventCounter(width=32)
        self.submodules.issued_counter = issued_counter = ImmediateEventCounter(width=28)
        self.submodules.transferred_counter = transferred_counter = ImmediateEventCounter(width=28)
        self.cycles = cycles = cycles_counter.value
        self.issued = issued = issued_counter.value
        self.transferred = transferred = transferred_counter.value
        self.cycles_counter = cycles_counter
        self.issued_counter = issued_counter
        self.transferred_counter = transferred_counter
        self.submodules.errors_counter = errors_counter = ImmediateEventCounter(width=32)
        self.errors = errors = errors_counter.value
        self.errors_counter = errors_counter
        self.first_bad=first_bad=Signal(address_width)
        self.observed=observed=Signal(32); self.expected=expected=Signal(32)
        self.failed=failed=Signal(); spacing=Signal(16); spacing_zero=Signal(reset=1); tail=Signal(6)
        self.spacing_zero = spacing_zero
        issue_active=Signal()
        self.issue_active = issue_active
        issue_last=Signal()
        penultimate_index=Signal(28)
        cmd_addr=Signal(address_width); check_addr=Signal(address_width)
        pattern=Signal(256); check=Signal(256)
        # Isolate return-buffer readiness from the descriptor's broader mode
        # fanout. The first read response follows command issue by several
        # cycles, so latching mode on start does not add request latency.
        self.read_response_enable = read_response_enable = Signal(reset=0)
        read_response_enable.attr.add("preserve")
        read_response_enable.attr.add("no_retiming")
        self.sync += If(start & ~busy, read_response_enable.eq(mode))
        # Registered cursors keep address addition off the checker/issue path.
        self.sync += If(start & ~busy, check_addr.eq(base))
        for i in range(8):
            key=(0x9e3779b9*(i+1)) & 0xffffffff
            self.comb += [pattern[i*32:(i+1)*32].eq(cmd_addr ^ seed ^ key),check[i*32:(i+1)*32].eq(check_addr ^ seed ^ key)]
        # issue_active is cleared on final issue and timeout, set only with a
        # nonzero-length start, and reset to zero; therefore it implies busy.
        can_issue=issue_active & spacing_zero
        self.comb += [r.enable.eq(1),r.sink.valid.eq(can_issue & mode),r.sink.address.eq(cmd_addr),r.sink.last.eq(issue_last),r.source.ready.eq(busy & mode),
            w.sink.valid.eq(can_issue & ~mode),w.sink.address.eq(cmd_addr),w.sink.data.eq(pattern),w.sink.last.eq(issue_last),
            p.cmd.valid.eq(Mux(mode,rp.cmd.valid,wp.cmd.valid)),p.cmd.we.eq(~mode),p.cmd.addr.eq(Mux(mode,rp.cmd.addr,wp.cmd.addr)),p.cmd.last.eq(issue_last),
            rp.cmd.ready.eq(p.cmd.ready & mode),wp.cmd.ready.eq(p.cmd.ready & ~mode),
            p.wdata.valid.eq(write_output.source.valid & ~mode),p.wdata.data.eq(write_output.source.data),p.wdata.we.eq(write_output.source.we),write_output.source.ready.eq(p.wdata.ready & ~mode),
            rp.rdata.valid.eq(p.rdata.valid & mode),rp.rdata.data.eq(p.rdata.data),p.rdata.ready.eq(rp.rdata.ready & read_response_enable)]
        accepted=Mux(mode,r.sink.valid & r.sink.ready,w.sink.valid & w.sink.ready)
        completed=Mux(mode,r.source.valid & r.source.ready,p.wdata.valid & p.wdata.ready)
        # accepted implies issue_active, which implies busy; therefore the
        # cursor load/increment priority can be expressed without busy.
        self.sync += If(start & ~busy, cmd_addr.eq(base)).Elif(
            accepted, cmd_addr.eq(cmd_addr + 1))
        self.comb += [
            cycles_counter.clear.eq(start & ~busy),
            issued_counter.clear.eq(start & ~busy),
            transferred_counter.clear.eq(start & ~busy),
            cycles_counter.event.eq(busy),
            issued_counter.event.eq(accepted),
            transferred_counter.event.eq(busy & completed),
        ]
        check_data=Signal(256, reset_less=True); checked_addr=Signal(address_width, reset_less=True); check_base=Signal(32, reset_less=True); check_valid=Signal()
        check_pattern=Signal(256)
        # Payload snapshots are meaningful only when check_valid is set.
        # Capture them every edge so busy/mode/completion do not drive three
        # wide FF enables; keep the validity/diagnostic qualification exact.
        self.sync += [check_valid.eq(busy & mode & completed),
            check_data.eq(r.source.data), checked_addr.eq(check_addr),
            check_base.eq(check_addr ^ seed)]
        for i in range(8):
            self.comb += check_pattern[i*32:(i+1)*32].eq(check_base ^ ((0x9e3779b9*(i+1)) & 0xffffffff))
        mismatch_lanes=Signal(8)
        verify_valid=Signal()
        verify_data=Signal(256,reset_less=True)
        verify_addr=Signal(address_width,reset_less=True)
        verify_base=Signal(32,reset_less=True)
        self.sync += [verify_valid.eq(check_valid),verify_data.eq(check_data),verify_addr.eq(checked_addr),verify_base.eq(check_base)]
        for i in range(8):
            self.sync += mismatch_lanes[i].eq(check_data[32*i:32*(i+1)] != check_pattern[32*i:32*(i+1)])
        mismatch=mismatch_lanes != 0
        self.comb += [
            errors_counter.clear.eq(start & ~busy),
            errors_counter.event.eq(busy & verify_valid & mismatch),
        ]
        bad_observed=Signal(32);bad_expected=Signal(32)
        self.comb += [bad_observed.eq(0),bad_expected.eq(0)]
        # Recreate the expected word from the captured shared base and the
        # lowest mismatching lane's constant key. This avoids selecting a
        # 32-bit word from the registered 256-bit expected pattern.
        prior = Constant(0, 1)
        expected_terms = []
        for i in range(8):
            lowest = mismatch_lanes[i] & ~prior
            prior = prior | mismatch_lanes[i]
            key = (0x9e3779b9 * (i + 1)) & 0xffffffff
            expected_terms.append(Replicate(lowest, 32) & Constant(key, 32))
        selected_key = Constant(0, 32)
        for term in expected_terms:
            selected_key = selected_key | term
        self.comb += bad_expected.eq(Mux(mismatch, verify_base ^ selected_key, 0))
        for i in reversed(range(8)):
            self.comb += If(mismatch_lanes[i],bad_observed.eq(verify_data[32*i:32*(i+1)]))
        counts_finished = Signal()
        completion_ready = Signal()
        counts_finished.attr.add("no_retiming")
        completion_ready.attr.add("no_retiming")
        self.sync += If(start & ~busy, counts_finished.eq(0), completion_ready.eq(0)).Else(
            counts_finished.eq((issued == length) & (transferred == length)),
            completion_ready.eq(counts_finished & self.drained))
        self.sync += If(start & ~busy,
            busy.eq(length!=0),command_select.eq(length!=0),write_select.eq(length!=0),read_select.eq(length!=0),done.eq(length==0),failed.eq(length==0),first_bad.eq(0),observed.eq(0),expected.eq(0),tail.eq(0),
            issue_active.eq(length!=0),issue_last.eq(length==1),penultimate_index.eq(length-2)
        ).Elif(busy,
            If(accepted,
                If(issue_last,issue_active.eq(0)
                ).Elif(issued==penultimate_index,issue_last.eq(1))
            ),
            If(completed,check_addr.eq(check_addr+1)),
            If(verify_valid & mismatch,failed.eq(1),
                If(errors==0,first_bad.eq(verify_addr),observed.eq(bad_observed),expected.eq(bad_expected))),
            # `tail` accumulates 32 cycles on which all issued/transferred
            # counts match and the external path reports drained. A low
            # drained cycle pauses this count; it does not reset it.
            If(completion_ready,
                tail.eq(tail+1),If(tail==31,busy.eq(0),command_select.eq(0),write_select.eq(0),read_select.eq(0),done.eq(1))),
            If(cycles==0x0fffffff,busy.eq(0),issue_active.eq(0),command_select.eq(0),write_select.eq(0),read_select.eq(0),done.eq(1),failed.eq(1))
        )
        # Preserve the original same-edge spacing load/decrement priority,
        # while removing it from the broad busy-controlled engine branch.
        self.sync += If(start & ~busy,
            spacing.eq(0), spacing_zero.eq(1)
        ).Elif(accepted,
            spacing.eq(gap), spacing_zero.eq(gap == 0)
        ).Elif(busy & (spacing != 0),
            spacing.eq(spacing - 1), spacing_zero.eq(spacing == 1)
        )

    def add_csrs(self, owner, bus, adr, values, allowed, address_select=None):
        selected=lambda n: (adr==n) if address_select is None else address_select[n]
        # Register the CSR request before the DMA consumes it. While this
        # pulse is pending, protect the descriptor from a following write.
        accepted_start = (bus.write & selected(64) & bus.writedata[0] & allowed &
            ~self.busy & ~self.start)
        self.start.attr.add("preserve")
        self.start.attr.add("no_retiming")
        owner.sync += self.start.eq(accepted_start)
        for n,s in [(65,self.base),(66,self.length),(67,self.mode),(68,self.seed),(69,self.gap)]:
            owner.sync += If(bus.write & selected(n) & ~self.busy & ~self.start,s.eq(bus.writedata))
        values.update({64:Cat(self.busy,self.done,self.failed),65:self.base,66:self.length,67:self.mode,68:self.seed,69:self.gap,70:self.cycles[:32],71:Constant(0,32),72:self.issued,73:self.transferred,74:self.errors,75:self.first_bad,76:self.observed,77:self.expected})
