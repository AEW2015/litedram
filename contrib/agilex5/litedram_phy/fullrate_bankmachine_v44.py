"""B44: B38 with synthesis-preserved, retimable bank-lock shadow."""
#
# This file is part of LiteDRAM.
#
# Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2016-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""V38: BankMachineV26 plus a valid-qualified-equivalent unqualified WE term."""

import math

from migen import *

from litex.soc.interconnect import stream
from litedram_phy.fullrate_retimed_txxd_v4 import RetimedTXXD

from litedram.common import *
from litedram.core.multiplexer import *

# AddressSlicer ------------------------------------------------------------------------------------

class _AddressSlicer:
    """Helper for extracting row/col from address

    Column occupies lower bits of the address, row - higher bits. Address has
    a forced alignment, so column does not contain alignment bits.
    """
    def __init__(self, colbits, address_align):
        self.colbits       = colbits
        self.address_align = address_align

    def row(self, address):
        split = self.colbits - self.address_align
        return address[split:]

    def col(self, address):
        split = self.colbits - self.address_align
        if self.colbits > 10:
            # A10 is reserved for auto-precharge, this bit needs to be skipped for col addresses.
            return Cat(
                Replicate(0, self.address_align),
                address[:10-self.address_align],
                Replicate(0, 1),
                address[10-self.address_align:split]
            )
        else:
            return Cat(
                Replicate(0, self.address_align),
                address[:split]
            )

# BankMachine --------------------------------------------------------------------------------------

class NonRefillingPayloadBuffer(Module):
    """One-entry hold register; sink readiness does not depend on source.ready."""
    def __init__(self, layout):
        self.sink = sink = stream.Endpoint(layout)
        self.source = source = stream.Endpoint(layout)
        payload = Signal(sum(field[1] for field in layout))
        self.comb += [sink.ready.eq(~source.valid), source.payload.raw_bits().eq(payload)]
        push = sink.valid & sink.ready
        pop = source.valid & source.ready
        self.sync += If(push, payload.eq(sink.payload.raw_bits()))
        self.sync += If(push, source.valid.eq(1)).Elif(pop, source.valid.eq(0))


class BankMachine(Module):
    """Converts requests from ports into DRAM commands

    BankMachine abstracts single DRAM bank by keeping track of the currently
    selected row. It converts requests from LiteDRAMCrossbar to targetted
    to that bank into DRAM commands that go to the Multiplexer, inserting any
    needed activate/precharge commands (with optional auto-precharge). It also
    keeps track and enforces some DRAM timings (other timings are enforced in
    the Multiplexer).

    BankMachines work independently from the data path (which connects
    LiteDRAMCrossbar with the Multiplexer directly).

    Stream of requests from LiteDRAMCrossbar is being queued, so that reqeust
    can be "looked ahead", and auto-precharge can be performed (if enabled in
    settings).

    Lock (cmd_layout.lock) is used to synchronise with LiteDRAMCrossbar. It is
    being held when:
     - there is a valid command awaiting in `cmd_buffer_lookahead` - this buffer
       becomes ready simply when the next data gets fetched to the `cmd_buffer`
     - there is a valid command in `cmd_buffer` - `cmd_buffer` becomes ready
       when the BankMachine sends wdata_ready/rdata_valid back to the crossbar

    Parameters
    ----------
    n : int
        Bank number
    address_width : int
        LiteDRAMInterface address width
    address_align : int
        Address alignment depending on burst length
    nranks : int
        Number of separate DRAM chips (width of chip select)
    settings : ControllerSettings
        LiteDRAMController settings

    Attributes
    ----------
    req : Record(cmd_layout)
        Stream of requests from LiteDRAMCrossbar
    refresh_req : Signal(), in
        Indicates that refresh needs to be done, connects to Refresher.cmd.valid
    refresh_gnt : Signal(), out
        Indicates that refresh permission has been granted, satisfying timings
    cmd : Endpoint(cmd_request_rw_layout)
        Stream of commands to the Multiplexer
    """
    def __init__(self, n, address_width, address_align, nranks, settings):
        self.req = req = Record(cmd_layout(address_width))
        self.refresh_req = refresh_req = Signal()
        self.refresh_gnt = refresh_gnt = Signal()

        a  = settings.geom.addressbits
        ba = settings.geom.bankbits + log2_int(nranks)
        self.cmd = cmd = stream.Endpoint(cmd_request_rw_layout(a, ba))
        cmd.pending_write = Signal()
        cmd.ready_read = Signal()
        cmd.ready_write = Signal()
        cmd.ready_activate = Signal()
        cmd.ready_precharge = Signal()

        # # #

        auto_precharge = Signal()

        # Command buffer ---------------------------------------------------------------------------
        cmd_buffer_layout    = [("we", 1), ("addr", len(req.addr))]
        from litedram_phy.fullrate_striped_v17 import OutputRegisteredFIFO
        cmd_buffer_lookahead = OutputRegisteredFIFO(
            cmd_buffer_layout, settings.cmd_buffer_depth,
            buffered=settings.cmd_buffer_buffered)
        cmd_buffer = NonRefillingPayloadBuffer(cmd_buffer_layout) # 1 depth, non-refilling payload hold
        self.submodules += cmd_buffer_lookahead, cmd_buffer
        # Track the exact request occupancy across lookahead and current
        # buffers. Admission adds one token and an accepted CAS removes one.
        # The registered zero flag equals the old OR of all storage valids.
        outstanding = Signal(max=settings.cmd_buffer_depth + 6)
        lock_nonzero = Signal(reset=0)
        lock_nonzero.attr.add("preserve_syn_only")
        push = req.valid & req.ready
        pop = req.wdata_ready | req.rdata_valid
        self.sync += [
            If(push & ~pop, outstanding.eq(outstanding + 1)).Elif(
                pop & ~push, outstanding.eq(outstanding - 1)),
            If(push, lock_nonzero.eq(1)).Elif(
                pop & (outstanding == 1), lock_nonzero.eq(0)),
        ]
        self.outstanding_requests = outstanding
        self.lock_reference = cmd_buffer_lookahead.nonempty | cmd_buffer.source.valid
        self.comb += cmd.pending_write.eq(cmd_buffer.source.we)
        self.comb += [
            req.connect(cmd_buffer_lookahead.sink, keep={"valid", "ready", "we", "addr"}),
            cmd_buffer_lookahead.source.connect(cmd_buffer.sink),
            cmd_buffer.source.ready.eq(req.wdata_ready | req.rdata_valid),
            req.lock.eq(lock_nonzero),
        ]

        slicer = _AddressSlicer(settings.geom.colbits, address_align)

        # Row tracking -----------------------------------------------------------------------------
        row        = Signal(settings.geom.rowbits)
        row_opened = Signal()
        row_hit    = Signal()
        row_open   = Signal()
        row_close  = Signal()
        if getattr(settings, "registered_row_hit", False):
            # This mode shortens the combinational path from the buffered
            # address to command arbitration. The capture event aligns the
            # registered comparison with the cycle in which Buffer.source
            # presents the captured request.
            assert not settings.with_auto_precharge, \
                "registered_row_hit requires with_auto_precharge=False"
            self.sync += If(row_close,
                row_hit.eq(0)
            ).Elif(row_open,
                row_hit.eq(1)
            ).Elif(
                cmd_buffer.sink.valid & cmd_buffer.sink.ready,
                row_hit.eq(row == slicer.row(cmd_buffer.sink.addr))
            )
        else:
            self.comb += row_hit.eq(row == slicer.row(cmd_buffer.source.addr))
        self.sync += \
            If(row_close,
                row_opened.eq(0)
            ).Elif(row_open,
                row_opened.eq(1),
                row.eq(slicer.row(cmd_buffer.source.addr))
            )

        # Address generation -----------------------------------------------------------------------
        row_col_n_addr_sel = Signal()
        self.comb += [
            cmd.ba.eq(n),
            If(row_col_n_addr_sel,
                cmd.a.eq(slicer.row(cmd_buffer.source.addr))
            ).Else(
                cmd.a.eq((auto_precharge << 10) | slicer.col(cmd_buffer.source.addr))
            )
        ]

        # tWTP (write-to-precharge) controller -----------------------------------------------------
        write_latency = math.ceil(settings.phy.cwl / settings.phy.nphases)
        precharge_time = write_latency + settings.timing.tWR + settings.timing.tCCD # AL=0
        self.submodules.twtpcon = twtpcon = RetimedTXXD(precharge_time)
        self.comb += twtpcon.valid.eq(req.wdata_ready)

        # tRC (activate-activate) controller -------------------------------------------------------
        self.submodules.trccon = trccon = tXXDController(settings.timing.tRC)
        # row_open is asserted only on the accepted ACTIVATE branch, which
        # also asserts cmd.valid. Drop this redundant FSM output from the
        # timing-feedback cone without changing the event cycle.
        self.comb += trccon.valid.eq(cmd.ready_activate & row_open)

        # tRAS (activate-precharge) controller -----------------------------------------------------
        self.submodules.trascon = trascon = tXXDController(settings.timing.tRAS)
        self.comb += trascon.valid.eq(cmd.ready_activate & row_open)

        # Auto Precharge generation ----------------------------------------------------------------
        # generate auto precharge when current and next cmds are to different rows
        if settings.with_auto_precharge:
            self.comb += \
                If(cmd_buffer_lookahead.source.valid & cmd_buffer.source.valid,
                    If(slicer.row(cmd_buffer_lookahead.source.addr) !=
                       slicer.row(cmd_buffer.source.addr),
                        auto_precharge.eq(row_close == 0)
                    )
                )

        # Control and command generation FSM -------------------------------------------------------
        # Note: tRRD, tFAW, tCCD, tWTR timings are enforced by the multiplexer
        self.submodules.fsm = fsm = FSM()
        cmd.cas_unqualified = Signal()
        cmd.ras_unqualified = Signal()
        cmd.we_unqualified = Signal()
        cmd.is_cmd_unqualified = Signal()
        cmd.is_read_unqualified = Signal()
        cmd.is_write_unqualified = Signal()
        we_regular_state = fsm.ongoing("REGULAR")
        we_precharge_state = fsm.ongoing("PRECHARGE")
        we_activate_state = fsm.ongoing("ACTIVATE")
        self.comb += [
            cmd.cas_unqualified.eq(we_regular_state),
            cmd.ras_unqualified.eq(we_precharge_state | we_activate_state),
            cmd.we_unqualified.eq(
                (we_regular_state & cmd.pending_write) | we_precharge_state),
            cmd.is_cmd_unqualified.eq(we_precharge_state | we_activate_state),
            cmd.is_read_unqualified.eq(we_regular_state & ~cmd.pending_write),
            cmd.is_write_unqualified.eq(we_regular_state & cmd.pending_write),
        ]
        fsm.act("REGULAR",
            If(refresh_req,
                NextState("REFRESH")
            ).Elif(cmd_buffer.source.valid,
                If(row_opened,
                    If(row_hit,
                        cmd.valid.eq(1),
                        If(cmd_buffer.source.we,
                            req.wdata_ready.eq(cmd.ready_write),
                            cmd.is_write.eq(1),
                            cmd.we.eq(1),
                        ).Else(
                            req.rdata_valid.eq(cmd.ready_read),
                            cmd.is_read.eq(1)
                        ),
                        cmd.cas.eq(1),
                        If(cmd.ready & auto_precharge,
                           NextState("AUTOPRECHARGE")
                        )
                    ).Else(  # row_opened & ~row_hit
                        NextState("PRECHARGE")
                    )
                ).Else(  # ~row_opened
                    NextState("ACTIVATE")
                )
            )
        )
        # DMA-local read recovery, in controller cycles (7.5 ns minimum).
        rtp = Signal(3)
        self.sync += If(req.rdata_valid, rtp.eq(3)).Elif(rtp != 0, rtp.eq(rtp - 1))
        fsm.act("PRECHARGE",
            # Fixed command class; validity alone carries timing eligibility.
            cmd.ras.eq(1), cmd.we.eq(1), cmd.is_cmd.eq(1),
            If(twtpcon.ready & trascon.ready & (rtp == 0),
                cmd.valid.eq(1),
                If(cmd.ready_precharge, NextState("TRP"))
            ),
            row_close.eq(1)
        )
        fsm.act("AUTOPRECHARGE",
            If(twtpcon.ready & trascon.ready & (rtp == 0),
                NextState("TRP")
            ),
            row_close.eq(1)
        )
        fsm.act("ACTIVATE",
            cmd.ras.eq(1), cmd.is_cmd.eq(1),
            If(trccon.ready,
                row_col_n_addr_sel.eq(1), row_open.eq(1), cmd.valid.eq(1),
                If(cmd.ready_activate, NextState("TRCD"))
            )
        )
        fsm.act("REFRESH",
            If(twtpcon.ready & trascon.ready & (rtp == 0),
                refresh_gnt.eq(1),
            ),
            row_close.eq(1),
            cmd.is_cmd.eq(1),
            If(~refresh_req,
                NextState("REGULAR")
            )
        )
        fsm.delayed_enter("TRP", "ACTIVATE", settings.timing.tRP - 1)
        fsm.delayed_enter("TRCD", "REGULAR", settings.timing.tRCD - 1)

BankMachineV38 = BankMachine
