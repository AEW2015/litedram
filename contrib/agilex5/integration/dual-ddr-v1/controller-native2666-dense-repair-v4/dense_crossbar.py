#
# This file is part of LiteDRAM.
#
# Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2016-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2018 John Sully <john@csquare.ca>
# SPDX-License-Identifier: BSD-2-Clause

"""LiteDRAM Crossbar with reset-masked local write-data selectors."""

from functools import reduce
from operator import or_

from migen import *
from migen.genlib import roundrobin

from litex.soc.interconnect import stream

from litedram.common import *
from litedram.core.controller import *
from litedram.frontend.adapter import *

# LiteDRAMCrossbar ---------------------------------------------------------------------------------

class LiteDRAMCrossbar(Module):
    """Multiplexes LiteDRAMController (slave) between ports (masters)

    To get a port to LiteDRAM, use the `get_port` method. It handles data width
    conversion and clock domain crossing, returning LiteDRAMNativePort.

    The crossbar routes requests from masters to the BankMachines
    (bankN.cmd_layout) and connects data path directly to the Multiplexer
    (data_layout). It performs address translation based on chosen
    `controller.settings.address_mapping`.
    Internally, all masters are multiplexed between controller banks based on
    the bank address (extracted from the presented address). Each bank has
    a RoundRobin arbiter, that selects from masters that want to access this
    bank and are not already locked.

    Locks (cmd_layout.lock) make sure that, when a master starts a transaction
    with given bank (which may include multiple reads/writes), no other bank
    will be assigned to it during this time.
    Arbiter (of a bank) considers given master as a candidate for selection if:
     - given master's command is valid
     - given master addresses the arbiter's bank
     - given master is not locked
       * i.e. it is not during transaction with another bank
       * i.e. no other bank's arbiter granted permission for this master (with
         bank.lock being active)

    Data ready/valid signals for banks are routed from bankmachines with
    a latency that synchronizes them with the data coming over datapath.

    Parameters
    ----------
    controller : LiteDRAMInterface
        Interface to LiteDRAMController

    Attributes
    ----------
    masters : [LiteDRAMNativePort, ...]
        LiteDRAM memory ports
    """
    def __init__(self, controller):
        self.controller = controller

        self.rca_bits         = controller.address_width
        self.nbanks           = controller.nbanks
        self.nranks           = controller.nranks
        self.cmd_buffer_depth = controller.settings.cmd_buffer_depth
        self.read_latency     = controller.settings.phy.read_latency + 1
        self.write_latency    = controller.settings.phy.write_latency + 1

        self.bank_bits = log2_int(self.nbanks, False)
        self.rank_bits = log2_int(self.nranks, False)

        self.masters = []

    def get_port(self, mode="both", data_width=None, clock_domain="sys", reverse=False):
        if self.finalized:
            raise FinalizeError

        if data_width is None:
            # use internal data_width when no width adaptation is requested
            data_width = self.controller.data_width

        # Crossbar port ----------------------------------------------------------------------------
        port = LiteDRAMNativePort(
            mode          = mode,
            address_width = self.rca_bits + self.bank_bits - self.rank_bits,
            data_width    = self.controller.data_width,
            clock_domain  = "sys",
            id            = len(self.masters))
        self.masters.append(port)

        # Clock domain crossing --------------------------------------------------------------------
        if clock_domain != "sys":
            new_port = LiteDRAMNativePort(
                mode          = mode,
                address_width = port.address_width,
                data_width    = port.data_width,
                clock_domain  = clock_domain,
                id            = port.id)
            self.submodules += LiteDRAMNativePortCDC(new_port, port)
            port = new_port

        # Data width conversion --------------------------------------------------------------------
        if data_width != self.controller.data_width:
            if data_width > self.controller.data_width:
                addr_shift = -log2_int(data_width//self.controller.data_width)
            else:
                addr_shift = log2_int(self.controller.data_width//data_width)
            new_port = LiteDRAMNativePort(
                mode          = mode,
                address_width = port.address_width + addr_shift,
                data_width    = data_width,
                clock_domain  = clock_domain,
                id            = port.id)
            self.submodules += ClockDomainsRenamer(clock_domain)(
                LiteDRAMNativePortConverter(new_port, port, reverse))
            port = new_port

        return port

    def do_finalize(self):
        controller = self.controller
        nmasters   = len(self.masters)

        # Address mapping --------------------------------------------------------------------------
        cba_shifts = {
            "ROW_BANK_COL": max(
                controller.settings.geom.colbits - controller.address_align,
                log2_int(getattr(controller.settings, "bank_byte_alignment", 0) //(controller.data_width // 8))
            )
        }
        cba_shift = cba_shifts[controller.settings.address_mapping]
        if (getattr(controller.settings, "with_agilex_fullrate_bankgroups", False) or
                getattr(controller.settings, "with_agilex_static_bg_ownership", False)):
            if (self.bank_bits != 4 or self.rank_bits != 0 or
                    controller.address_align != 3 or self.rca_bits != 24):
                raise ValueError("Agilex full-rate mapping requires x32 BL8, one rank, four bank bits and 24 RCA bits")
            # Native 32-byte word address: BG[1:0], column[6:0], BA[1:0], row[16:0].
            # Every master uses the same permutation so CPU and DMA aliases agree.
            m_ba = [Cat(m.cmd.addr[9:11], m.cmd.addr[0:2]) for m in self.masters]
            m_rca = [Cat(m.cmd.addr[2:9], m.cmd.addr[11:]) for m in self.masters]
        else:
            m_ba      = [m.get_bank_address(self.bank_bits, cba_shift)for m in self.masters]
            m_rca     = [m.get_row_column_address(self.bank_bits, self.rca_bits, cba_shift) for m in self.masters]

        if getattr(controller.settings, "with_agilex_static_bg_ownership", False):
            self._do_finalize_static_bg(controller, m_ba, m_rca)
            return

        master_readys       = [0]*nmasters
        master_wdata_readys = [0]*nmasters
        master_rdata_valids = [0]*nmasters

        arbiters = [roundrobin.RoundRobin(nmasters, roundrobin.SP_CE) for n in range(self.nbanks)]
        self.submodules += arbiters

        for nb, arbiter in enumerate(arbiters):
            bank = getattr(controller, "bank"+str(nb))

            # For each master, determine if another bank locks it ----------------------------------
            master_locked = []
            for nm, master in enumerate(self.masters):
                locked = Signal()
                for other_nb, other_arbiter in enumerate(arbiters):
                    if other_nb != nb:
                        other_bank = getattr(controller, "bank"+str(other_nb))
                        locked = locked | (other_bank.lock & (other_arbiter.grant == nm))
                master_locked.append(locked)

            # Arbitrate ----------------------------------------------------------------------------
            bank_selected  = [(ba == nb) & ~locked for ba, locked in zip(m_ba, master_locked)]
            bank_requested = [bs & master.cmd.valid for bs, master in zip(bank_selected, self.masters)]
            self.comb += [
                arbiter.request.eq(Cat(*bank_requested)),
                arbiter.ce.eq(~bank.valid & ~bank.lock)
            ]

            # Route requests -----------------------------------------------------------------------
            self.comb += [
                bank.addr.eq(Array(m_rca)[arbiter.grant]),
                bank.we.eq(Array(self.masters)[arbiter.grant].cmd.we),
                bank.valid.eq(Array(bank_requested)[arbiter.grant])
            ]
            master_readys = [master_ready | ((arbiter.grant == nm) & bank_selected[nm] & bank.ready)
                for nm, master_ready in enumerate(master_readys)]
            master_wdata_readys = [master_wdata_ready | ((arbiter.grant == nm) & bank.wdata_ready)
                for nm, master_wdata_ready in enumerate(master_wdata_readys)]
            master_rdata_valids = [master_rdata_valid | ((arbiter.grant == nm) & bank.rdata_valid)
                for nm, master_rdata_valid in enumerate(master_rdata_valids)]

        # Delay write/read signals based on their latency. Decode the exact
        # one-hot ready vector alongside its last existing delay register so
        # wide data/mask selection uses local registered selects.
        master_wdata_readys = self._delay_and_route_wdata(
            controller, master_wdata_readys)

        for nm, master_rdata_valid in enumerate(master_rdata_valids):
            for i in range(self.read_latency):
                new_master_rdata_valid = Signal()
                self.sync += new_master_rdata_valid.eq(master_rdata_valid)
                master_rdata_valid = new_master_rdata_valid
            master_rdata_valids[nm] = master_rdata_valid

        for master, master_ready in zip(self.masters, master_readys):
            self.comb += master.cmd.ready.eq(master_ready)
        for master, master_wdata_ready in zip(self.masters, master_wdata_readys):
            self.comb += master.wdata.ready.eq(master_wdata_ready)
        for master, master_rdata_valid in zip(self.masters, master_rdata_valids):
            self.comb += master.rdata.valid.eq(master_rdata_valid)

        # Route data reads -------------------------------------------------------------------------
        for master in self.masters:
            self.comb += master.rdata.data.eq(controller.rdata)

    def _delay_and_route_wdata(self, controller, master_wdata_readys):
        """Preserve ready latency while localizing the final data selection.

        The original wide Case selected a master only for an exactly one-hot
        final ready vector and selected zero for both zero and multi-hot
        vectors. Decode that same exact predicate before the last existing
        ready-delay edge, replicate its register near each 32-bit data slice
        and mask, and use the registered copies to mux the wide payload.
        """
        nmasters = len(self.masters)
        if len(controller.wdata) % 32:
            raise ValueError("controller write-data width must be a multiple of 32 bits")
        pre_final_ready = []
        final_ready = []
        for nm, master_wdata_ready in enumerate(master_wdata_readys):
            for i in range(self.write_latency):
                if i == self.write_latency - 1:
                    pre_final_ready.append(master_wdata_ready)
                new_master_wdata_ready = Signal()
                self.sync += new_master_wdata_ready.eq(master_wdata_ready)
                master_wdata_ready = new_master_wdata_ready
            final_ready.append(master_wdata_ready)

        ready_before_final = Cat(*pre_final_ready)
        local_selects = []
        select_copies = len(controller.wdata) // 32 + 1
        for copy in range(select_copies):
            select = Signal(nmasters, reset_less=True,
                name=f"crossbar_wdata_select_local_{copy}")
            select.attr.add("preserve")
            select.attr.add("no_retiming")
            select_valid = Signal(reset=0,
                name=f"crossbar_wdata_select_valid_{copy}")
            select_valid.attr.add("preserve")
            select_valid.attr.add("no_retiming")
            self.sync += [
                select.eq(Cat(*[
                    ready_before_final == (1 << nm) for nm in range(nmasters)])),
                select_valid.eq(1),
            ]
            qualified_select = Signal(nmasters,
                name=f"crossbar_wdata_select_qualified_{copy}")
            self.comb += qualified_select.eq(Mux(select_valid, select, 0))
            local_selects.append(qualified_select)

        data_slices = len(controller.wdata) // 32
        for word in range(data_slices):
            lo, hi = word * 32, (word + 1) * 32
            self.comb += controller.wdata[lo:hi].eq(reduce(or_, [
                Mux(local_selects[word][nm], master.wdata.data[lo:hi], 0)
                for nm, master in enumerate(self.masters)]))
        self.comb += controller.wdata_we.eq(reduce(or_, [
            Mux(local_selects[-1][nm], master.wdata.we, 0)
            for nm, master in enumerate(self.masters)]))
        return final_ready

    def _do_finalize_static_bg(self, controller, m_ba, m_rca):
        """Route each BG's banks directly to its striped native master."""
        if (len(self.masters) != 4 or self.bank_bits != 4 or self.rank_bits != 0 or
                controller.address_align != 3 or self.rca_bits != 24 or
                controller.data_width != 256 or
                any(master.data_width != 256 for master in self.masters)):
            raise ValueError("Static BG ownership requires four x32 256-bit native masters")

        master_readys = []
        master_wdata_readys = []
        master_rdata_valids = []
        due_lookaheads = []
        self.comb += controller.rdata_start.eq(reduce(or_, [getattr(controller,"bank"+str(nb)).rdata_valid for nb in range(self.nbanks)]))

        for nb in range(self.nbanks):
            bank = getattr(controller, "bank" + str(nb))
            owner = nb >> 2
            master = self.masters[owner]
            other_locks = [getattr(controller, "bank" + str(other_nb)).lock
                for other_nb in range(owner*4, owner*4 + 4) if other_nb != nb]
            unlocked = ~reduce(or_, other_locks)
            selected = m_ba[owner] == nb
            self.comb += [
                bank.addr.eq(m_rca[owner]),
                bank.we.eq(master.cmd.we),
                bank.valid.eq(master.cmd.valid & selected & unlocked),
            ]

        for nm, master in enumerate(self.masters):
            owned_banks = range(nm*4, nm*4 + 4)
            ready_terms = []
            write_terms = []
            read_terms = []
            for nb in owned_banks:
                bank = getattr(controller, "bank" + str(nb))
                other_locks = [getattr(controller, "bank" + str(other_nb)).lock
                    for other_nb in owned_banks if other_nb != nb]
                selected = (m_ba[nm] == nb)
                ready_terms.append(selected & ~reduce(or_, other_locks) & bank.ready)
                write_terms.append(bank.wdata_ready)
                read_terms.append(bank.rdata_valid)
            master_readys.append(reduce(or_, ready_terms))
            master_wdata_readys.append(reduce(or_, write_terms))
            master_rdata_valids.append(reduce(or_, read_terms))

        # Preserve native ready/valid latency. The write-data mux is decoded
        # beside the last existing ready-delay stage by the shared helper.
        master_wdata_readys = self._delay_and_route_wdata(
            controller, master_wdata_readys)
        for nm, master in enumerate(self.masters):
            rdata_valid = master_rdata_valids[nm]
            for stage in range(self.read_latency):
                if stage == self.read_latency-1:
                    due_lookaheads.append(rdata_valid)
                delayed = Signal()
                self.sync += delayed.eq(rdata_valid)
                rdata_valid = delayed
            master_rdata_valids[nm] = rdata_valid

            self.comb += [
                master.cmd.ready.eq(master_readys[nm]),
                master.wdata.ready.eq(master_wdata_readys[nm]),
                master.rdata.valid.eq(master_rdata_valids[nm]),
                master.rdata.data.eq(controller.rdata),
            ]

        self.comb += controller.rdata_due_lookahead.eq(reduce(or_,due_lookaheads))
        # Same edge/value as the OR of the four existing final qualifiers.
        self.sync += controller.rdata_due.eq(controller.rdata_due_lookahead)
