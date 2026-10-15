# Full-burst board contract: DFI mask pins are inactive. The generator
# retains partial-write diagnostics with an accepted-native-mask guard.
#
# This file is part of LiteDRAM.
#
# Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2016-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2018 John Sully <john@csquare.ca>
# SPDX-License-Identifier: BSD-2-Clause

"""V32: Registered per-bank qualified DFI events retimed across event mux. Per-bank address snapshots with registered one-hot selection retimed across DFI address mux. V17 with grouped parallel-OR DFI event selection."""

import math
from functools import reduce
from operator import or_, and_

from migen import *
from migen.genlib.roundrobin import *
from migen.genlib.coding import Decoder

from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import AutoCSR

from litedram.common import *
from litedram.core.bandwidth import Bandwidth

# _CommandChooser ----------------------------------------------------------------------------------

class AgilexBankGroupCooldown(Module):
    """Admit one CAS per CK/4 cycle and separate same-BG CAS by two cycles."""
    def __init__(self, bankbits=4):
        if bankbits != 4:
            raise ValueError("Agilex DDR4 scheduling requires four bank bits")
        self.cas = Signal()
        self.bank = Signal(bankbits)
        self.eligible = Signal(16)
        cooldown = [Signal(name="bg{}_cooldown".format(i)) for i in range(4)]
        self.comb += self.eligible.eq(Cat(*[
            ~Array(cooldown)[i >> 2] for i in range(16)]))
        for group, timer in enumerate(cooldown):
            self.sync += If(self.cas & (self.bank[2:4] == group),
                timer.eq(1)
            ).Else(timer.eq(0))


class RASPermissionPipeline(Module):
    """Preserve V7 RAS-permission cycles with independent registered inputs."""
    def __init__(self):
        self.timer_ready = Signal()
        self.accepted_act = Signal()
        self.allowed = Signal(reset=1)
        self.timer_rearm = Signal(reset=1)
        self.accepted_act_q = Signal(reset=0)
        for signal in (self.allowed, self.timer_rearm, self.accepted_act_q):
            signal.attr.add("no_retiming")
        self.comb += self.allowed.eq(self.timer_rearm & ~self.accepted_act_q)
        self.sync += [
            self.timer_rearm.eq(self.timer_ready),
            self.accepted_act_q.eq(self.accepted_act),
        ]

class _CommandChooser(Module):
    """Arbitrates between requests, filtering them based on their type

    Uses RoundRobin to choose current request, filters requests based on
    `want_*` signals.

    Parameters
    ----------
    requests : [Endpoint(cmd_request_rw_layout), ...]
        Request streams to consider for arbitration

    Attributes
    ----------
    want_reads : Signal, in
        Consider read requests
    want_writes : Signal, in
        Consider write requests
    want_cmds : Signal, in
        Consider command requests (without ACT)
    want_activates : Signal, in
        Also consider ACT commands
    cmd : Endpoint(cmd_request_rw_layout)
        Currently selected request stream (when ~cmd.valid, cas/ras/we are 0)
    """
    def __init__(self, requests, eligible=None, bank_group=False):
        self.want_reads     = Signal()
        self.want_writes    = Signal()
        self.want_cmds      = Signal()
        self.want_activates = Signal()

        a  = len(requests[0].a)
        ba = len(requests[0].ba)

        # cas/ras/we are 0 when valid is inactive
        self.cmd = cmd = stream.Endpoint(cmd_request_rw_layout(a, ba))

        # # #

        n = len(requests)

        valids = Signal(n)
        for i, request in enumerate(requests):
            is_act_cmd = request.ras & ~request.cas & ~request.we
            command = request.is_cmd & self.want_cmds & (~is_act_cmd | self.want_activates)
            read = request.is_read == self.want_reads
            write = request.is_write == self.want_writes
            self.comb += valids[i].eq(request.valid & (command | (read & write)))


        arbiter = RoundRobin(n, SP_CE)
        self.submodules += arbiter
        choices = Array(valids[i] for i in range(n))
        selected_valid = choices[arbiter.grant]
        if eligible is None:
            self.comb += arbiter.request.eq(valids)
            self.comb += cmd.valid.eq(selected_valid)
        else:
            # Filter blocked banks and, on an accepted CAS, steer the next
            # registered grant away from the just-used BG. This avoids a
            # grant bubble when another bank in that BG is also pending.
            selected_eligible = Array(eligible)[arbiter.grant]
            eligible_requests = []
            for i in range(n):
                request = valids[i] & eligible[i]
                if bank_group:
                    request = request & ~(cmd.valid & cmd.ready &
                        (requests[i].ba[2:4] == cmd.ba[2:4]))
                eligible_requests.append(request)
            self.comb += arbiter.request.eq(Cat(*eligible_requests))
            self.comb += cmd.valid.eq(selected_valid & selected_eligible)

        for name in ["a", "ba", "is_read", "is_write", "is_cmd"]:
            choices = Array(getattr(req, name) for req in requests)
            self.comb += getattr(cmd, name).eq(choices[arbiter.grant])

        for name in ["cas", "ras", "we"]:
            # we should only assert those signals when valid is 1
            choices = Array(getattr(req, name) for req in requests)
            self.comb += \
                If(cmd.valid,
                    getattr(cmd, name).eq(choices[arbiter.grant])
                )

        for i, request in enumerate(requests):
            self.comb += \
                If(cmd.valid & cmd.ready & (arbiter.grant == i),
                    request.ready.eq(1)
                )
        # Arbitrate if a command is being accepted or if the command is not valid to ensure a valid
        # command is selected when cmd.ready goes high.
        self.comb += arbiter.ce.eq(cmd.ready | ~cmd.valid)

    # helpers
    def accept(self):
        return self.cmd.valid & self.cmd.ready

    def activate(self):
        return self.cmd.ras & ~self.cmd.cas & ~self.cmd.we

    def write(self):
        return self.cmd.is_write

    def read(self):
        return self.cmd.is_read

# _Steerer -----------------------------------------------------------------------------------------

(STEER_NOP, STEER_CMD, STEER_REQ, STEER_REFRESH) = range(4)

class _Steerer(Module):
    """Connects selected request to DFI interface

    cas/ras/we/is_write/is_read are connected only when `cmd.valid & cmd.ready`.
    Rank bits are decoded and used to drive cs_n in multi-rank systems,
    STEER_REFRESH always enables all ranks.

    Parameters
    ----------
    commands : [Endpoint(cmd_request_rw_layout), ...]
        Command streams to choose from. Must be of len=4 in the order:
            NOP, CMD, REQ, REFRESH
        NOP can be of type Record(cmd_request_rw_layout) instead, so that it is
        always considered invalid (because of lack of the `valid` attribute).
    dfi : dfi.Interface
        DFI interface connected to PHY

    Attributes
    ----------
    sel : [Signal(max=len(commands)), ...], in
        Signals for selecting which request gets connected to the corresponding
        DFI phase. The signals should take one of the values from STEER_* to
        select given source.
    """
    def __init__(self, commands, dfi, bank_requests=None, bank_selects=None,
            bank_events=None, refresh_events=None, bank_wants=None):
        ncmd = len(commands)
        nph  = len(dfi.phases)
        self.sel = [Signal(max=ncmd) for i in range(nph)]

        # In full-rate 4x4 bank-group mode, snapshot each bank's address and
        # the exact source/bank selection on the same edge where V18 captures
        # the final DFI address. Recombine only the address after that edge;
        # all command/event pins retain the original steerer registers.
        address_snapshots = None
        address_refresh_snapshot = None
        address_nop_snapshot = None
        bank_select_snapshot = None
        phase_bank_selectors = None
        address_refresh_selectors = None
        address_nop_selectors = None
        event_snapshots = None
        refresh_event_snapshots = None
        if bank_requests is not None or bank_selects is not None:
            if bank_requests is None or bank_selects is None:
                raise ValueError("bank_requests and bank_selects must be supplied together")
            if len(bank_requests) != 16 or len(bank_selects) != 16:
                raise ValueError("snapshot retiming requires 16 bank requests/selects")
            if bank_events is None or refresh_events is None or bank_wants is None:
                raise ValueError("bank raw fields, wants, and refresh events are required with address snapshots")
            if set(bank_events) != {"valid", "cas", "ras", "we", "is_cmd", "is_read", "is_write"}:
                raise ValueError("bank raw fields must include valid, cas, ras, we, is_cmd, is_read, is_write")
            if set(bank_wants) != {"cmds", "reads", "writes", "activates"}:
                raise ValueError("bank wants must include cmds, reads, writes, activates")
            address_snapshots = [Signal(len(request.a), reset=0,
                name="steerer_bank{}_address_snapshot".format(i))
                for i, request in enumerate(bank_requests)]
            for snapshot in address_snapshots:
                snapshot.attr.add("preserve_syn_only")
            self.sync += [snapshot.eq(request.a)
                for snapshot, request in zip(address_snapshots, bank_requests)]
            address_refresh_snapshot = Signal(len(commands[STEER_REFRESH].a),
                reset=0, name="steerer_refresh_address_snapshot")
            address_refresh_snapshot.attr.add("preserve_syn_only")
            self.sync += address_refresh_snapshot.eq(commands[STEER_REFRESH].a)
            address_nop_snapshot = Signal(len(commands[STEER_NOP].a),
                reset=0, name="steerer_nop_address_snapshot")
            address_nop_snapshot.attr.add("preserve_syn_only")
            self.sync += address_nop_snapshot.eq(commands[STEER_NOP].a)
            bank_select_snapshot = Signal(16, reset=0,
                name="steerer_bank_address_select_snapshot")
            bank_select_snapshot.attr.add("preserve_syn_only")
            phase_bank_selectors = [Signal(reset=0,
                name="steerer_phase{}_bank_address_enable".format(p))
                for p in range(nph)]
            address_refresh_selectors = [Signal(reset=0,
                name="steerer_phase{}_refresh_address_select".format(p))
                for p in range(nph)]
            address_nop_selectors = [Signal(reset=1,
                name="steerer_phase{}_nop_address_select".format(p))
                for p in range(nph)]
            for selector in phase_bank_selectors + address_refresh_selectors + address_nop_selectors:
                selector.attr.add("preserve_syn_only")
            self.sync += [bank_select_snapshot[i].eq(bank_selects[i])
                for i in range(16)]
            for p, sel in enumerate(self.sel):
                self.sync += [
                    phase_bank_selectors[p].eq(
                        (sel == STEER_CMD) | (sel == STEER_REQ)),
                    address_refresh_selectors[p].eq(sel == STEER_REFRESH),
                    address_nop_selectors[p].eq(sel == STEER_NOP),
                ]
            event_snapshots = {}
            for name, expressions in bank_events.items():
                if len(expressions) != 16:
                    raise ValueError("each bank event vector must have 16 entries")
                snapshots = [Signal(reset=0,
                    name="steerer_bank{}_{}_event_snapshot".format(i, name))
                    for i in range(16)]
                for snapshot in snapshots:
                    snapshot.attr.add("preserve_syn_only")
                self.sync += [snapshot.eq(expression)
                    for snapshot, expression in zip(snapshots, expressions)]
                event_snapshots[name] = snapshots
            want_snapshots = {}
            for name, expression in bank_wants.items():
                snapshot = Signal(reset=0, name="steerer_want_{}_snapshot".format(name))
                snapshot.attr.add("preserve_syn_only")
                self.sync += snapshot.eq(expression)
                want_snapshots[name] = snapshot
            if set(refresh_events) != {"cas", "ras", "we", "read", "write"}:
                raise ValueError("refresh event expressions must include cas, ras, we, read, write")
            refresh_event_snapshots = {}
            for name, expression in refresh_events.items():
                snapshot = Signal(reset=0,
                    name="steerer_refresh_{}_event_snapshot".format(name))
                snapshot.attr.add("preserve_syn_only")
                self.sync += snapshot.eq(expression)
                refresh_event_snapshots[name] = snapshot

        # # #

        def valid_and(cmd, attr):
            if not hasattr(cmd, "valid"):
                return 0
            elif attr == "is_read" and hasattr(cmd, "_selected_is_read"):
                return cmd._selected_is_read if getattr(cmd, "_steerer_ready_is_constant", False) else cmd.ready & cmd._selected_is_read
            elif attr == "is_write" and hasattr(cmd, "_selected_is_write"):
                return cmd._selected_is_write if getattr(cmd, "_steerer_ready_is_constant", False) else cmd.ready & cmd._selected_is_write
            elif attr in ("cas", "ras", "we") and hasattr(cmd, "_selected_" + attr + "_valid"):
                helper = getattr(cmd, "_selected_" + attr + "_valid")
                return helper if getattr(cmd, "_steerer_ready_is_constant", False) else cmd.ready & helper
            else:
                return cmd.valid & cmd.ready & getattr(cmd, attr)

        # Several selector values can point at one endpoint (the common CMD/REQ
        # alias is indices 1 and 2). Combine those selectors before gating the
        # accepted event, then OR across distinct endpoints. This keeps event
        # selection parallel without changing duplicate-object semantics.
        def selected_event(attr, sel):
            groups = []
            by_identity = {}
            for index, cmd in enumerate(commands):
                key = id(cmd)
                if key not in by_identity:
                    by_identity[key] = [cmd, []]
                    groups.append(by_identity[key])
                by_identity[key][1].append(index)
            terms = []
            for cmd, indices in groups:
                selected = reduce(or_, [sel == index for index in indices])
                terms.append(selected & valid_and(cmd, attr))
            return reduce(or_, terms)

        for i, (phase, sel) in enumerate(zip(dfi.phases, self.sel)):
            nranks   = len(phase.cs_n)
            rankbits = log2_int(nranks)
            if hasattr(phase, "reset_n"):
                self.comb += phase.reset_n.eq(1)
            self.comb += phase.cke.eq(Replicate(Signal(reset=1), nranks))
            if hasattr(phase, "odt"):
                # FIXME: add dynamic drive for multi-rank (will be needed for high frequencies)
                self.comb += phase.odt.eq(Replicate(Signal(reset=1), nranks))
            if rankbits:
                rank_decoder = Decoder(nranks)
                self.submodules += rank_decoder
                self.comb += rank_decoder.i.eq((Array(cmd.ba[-rankbits:] for cmd in commands)[sel]))
                if i == 0: # Select all ranks on refresh.
                    self.sync += If(sel == STEER_REFRESH, phase.cs_n.eq(0)).Else(phase.cs_n.eq(~rank_decoder.o))
                else:
                    self.sync += phase.cs_n.eq(~rank_decoder.o)
                self.sync += phase.bank.eq(Array(cmd.ba[:-rankbits] for cmd in commands)[sel])
            else:
                self.sync += phase.cs_n.eq(0)
                self.sync += phase.bank.eq(Array(cmd.ba[:] for cmd in commands)[sel])

            if address_snapshots is None:
                self.sync += phase.address.eq(Array(cmd.a for cmd in commands)[sel])
            else:
                address_terms = [Mux(bank_select_snapshot[bank],
                    address_snapshots[bank], 0)
                    for bank in range(16)]
                bank_address_value = reduce(or_, address_terms)
                address_value = Mux(phase_bank_selectors[i], bank_address_value, 0)
                address_value = address_value | Mux(
                    address_refresh_selectors[i], address_refresh_snapshot, 0)
                address_value = address_value | Mux(
                    address_nop_selectors[i], address_nop_snapshot, 0)
                self.comb += phase.address.eq(address_value)
            if event_snapshots is None:
                self.sync += [
                    phase.cas_n.eq(~selected_event("cas", sel)),
                    phase.ras_n.eq(~selected_event("ras", sel)),
                    phase.we_n.eq(~selected_event("we", sel)),
                    phase.rddata_en.eq(selected_event("is_read", sel)),
                    phase.wrdata_en.eq(selected_event("is_write", sel)),
                ]
            else:
                def selected_snapshot(name):
                    terms = [Mux(bank_select_snapshot[bank],
                        event_snapshots[name][bank], 0)
                        for bank in range(16)]
                    return reduce(or_, terms)

                raw_valid = selected_snapshot("valid")
                raw_cas = selected_snapshot("cas")
                raw_ras = selected_snapshot("ras")
                raw_we = selected_snapshot("we")
                raw_is_cmd = selected_snapshot("is_cmd")
                raw_is_read = selected_snapshot("is_read")
                raw_is_write = selected_snapshot("is_write")
                accepted_command = raw_valid & raw_is_cmd & want_snapshots["cmds"]
                accepted_read = raw_valid & raw_is_read & want_snapshots["reads"]
                accepted_write = raw_valid & raw_is_write & want_snapshots["writes"]
                accepted_any = accepted_command | accepted_read | accepted_write
                is_activate = raw_ras & ~raw_cas & ~raw_we
                accepted_ras = raw_valid & raw_ras & (
                    (accepted_command & ~is_activate) |
                    (accepted_command & want_snapshots["activates"] & is_activate) |
                    accepted_read | accepted_write)
                accepted_cas = raw_valid & raw_cas & accepted_any
                accepted_we = raw_valid & raw_we & accepted_any
                def selected_event_or_refresh(name, bank_value):
                    bank_event = phase_bank_selectors[i] & bank_value
                    return bank_event | Mux(address_refresh_selectors[i],
                        refresh_event_snapshots[name], 0)
                self.comb += [
                    phase.cas_n.eq(~selected_event_or_refresh("cas", accepted_cas)),
                    phase.ras_n.eq(~selected_event_or_refresh("ras", accepted_ras)),
                    phase.we_n.eq(~selected_event_or_refresh("we", accepted_we)),
                    phase.rddata_en.eq(selected_event_or_refresh("read", accepted_read)),
                    phase.wrdata_en.eq(selected_event_or_refresh("write", accepted_write)),
                ]


# Multiplexer --------------------------------------------------------------------------------------

class Multiplexer(Module, AutoCSR):
    """Multplexes requets from BankMachines to DFI

    This module multiplexes requests from BankMachines (and Refresher) and
    connects them to DFI. Refresh commands are coordinated between the Refresher
    and BankMachines to ensure there are no conflicts. Enforces required timings
    between commands (some timings are enforced by BankMachines).

    Parameters
    ----------
    settings : ControllerSettings
        Controller settings (with .phy, .geom and .timing settings)
    bank_machines : [BankMachine, ...]
        Bank machines that generate command requests to the Multiplexer
    refresher : Refresher
        Generates REFRESH command requests
    dfi : dfi.Interface
        DFI connected to the PHY
    interface : LiteDRAMInterface
        Data interface connected directly to LiteDRAMCrossbar
    """
    def __init__(self,
            settings,
            bank_machines,
            refresher,
            dfi,
            interface):
        assert(settings.phy.nphases == len(dfi.phases))

        self.submodules.ras_permission = ras_permission = RASPermissionPipeline()
        ras_allowed = ras_permission.allowed
        self.ras_allowed = ras_permission.allowed
        self.ras_timer_rearm = ras_permission.timer_rearm
        self.ras_accepted_act = ras_permission.accepted_act_q
        cas_allowed = Signal(reset=1)

        # Read/Write Cmd/Dat phases ----------------------------------------------------------------
        nphases = settings.phy.nphases
        rdphase = settings.phy.rdphase
        wrphase = settings.phy.wrphase

        # Command choosing -------------------------------------------------------------------------
        requests = [bm.cmd for bm in bank_machines]
        fullrate_bg = getattr(settings, "with_agilex_fullrate_bankgroups", False)
        bg_cooldown = None
        if fullrate_bg:
            if settings.phy.nphases != 4 or settings.geom.bankbits != 4:
                raise ValueError("Agilex full-rate bank-group scheduling requires four phases and four bank bits")
            bg_cooldown = AgilexBankGroupCooldown(4)
            self.submodules.bg_cooldown = bg_cooldown
            # Bank IDs are BG[1:0] in the high two bits and BA[1:0] in
            # the low two. Hold BA constant while walking all four BGs.
            bank_order = [bank_machines[bg*4 + ba]
                for ba in range(4) for bg in range(4)]
            requests = [bm.cmd for bm in bank_order]
            eligible = [bg_cooldown.eligible[bg*4 + ba]
                for ba in range(4) for bg in range(4)]
        else:
            eligible = None
        self.submodules.choose_cmd = choose_cmd = _CommandChooser(requests,
            eligible=eligible, bank_group=fullrate_bg)
        # Fixed-slot choosers already decode live raw-valid read/write events.
        # Reuse those terms in _Steerer instead of rebuilding cmd.valid/class
        # selection for the DFI data enables.
        if hasattr(choose_cmd, "_selected_is_read"):
            choose_cmd.cmd._selected_is_read = choose_cmd._selected_is_read
        if hasattr(choose_cmd, "_selected_is_write"):
            choose_cmd.cmd._selected_is_write = choose_cmd._selected_is_write
        for field in ("cas", "ras", "we"):
            helper = "_selected_" + field + "_valid"
            if hasattr(choose_cmd, helper):
                setattr(choose_cmd.cmd, helper, getattr(choose_cmd, helper))
        if fullrate_bg:
            # READ/WRITE FSM states set ready to one whenever this endpoint
            # drives a DFI phase in the full-rate fixed-slot schedule.
            choose_cmd.cmd._steerer_ready_is_constant = True
        # ACT/PRE/CAS share one selection state and one physical DFI phase.
        # This removes the old one-frame command/data phase split.
        choose_req = choose_cmd
        self.choose_req = choose_req
        if settings.phy.nphases == 1:
            # When only 1 phase, use choose_req for all requests
            self.comb += choose_req.want_cmds.eq(1)
            self.comb += choose_req.want_activates.eq(ras_allowed)
        else:
            self.comb += [
                choose_cmd.want_cmds.eq(1),
                choose_cmd.want_activates.eq(ras_allowed),
            ]

        # Command steering -------------------------------------------------------------------------
        nop = Record(cmd_request_layout(settings.geom.addressbits,
                                        log2_int(len(bank_machines))))
        # nop must be 1st
        refresh_cmd = stream.Endpoint(cmd_request_rw_layout(
            settings.geom.addressbits, log2_int(len(bank_machines))))
        refresh_cmd.ready.reset = 1
        refresh_cmd.ready.attr.add("preserve")
        refresh_cmd.ready.attr.add("no_retiming")
        for signal in (refresh_cmd.valid, refresh_cmd.last, refresh_cmd.a,
                refresh_cmd.ba, refresh_cmd.cas, refresh_cmd.ras, refresh_cmd.we):
            signal.attr.add("preserve")
            signal.attr.add("no_retiming")
        self.refresh_cmd = refresh_cmd
        # Capture command fields on the ready/valid handshake. LiteDRAM's
        # sequence completion marker may accompany valid=0, so qualify it
        # with ready alone; the final valid command has already been captured.
        self.sync += [
            refresh_cmd.valid.eq(refresher.cmd.valid & refresher.cmd.ready),
            refresh_cmd.last.eq(refresher.cmd.last & refresher.cmd.ready),
            refresh_cmd.a.eq(refresher.cmd.a),
            refresh_cmd.ba.eq(refresher.cmd.ba),
            refresh_cmd.cas.eq(refresher.cmd.cas),
            refresh_cmd.ras.eq(refresher.cmd.ras),
            refresh_cmd.we.eq(refresher.cmd.we),
        ]
        self.comb += refresh_cmd.ready.eq(1)
        commands = [nop, choose_cmd.cmd, choose_cmd.cmd, refresh_cmd]
        if fullrate_bg:
            if (len(requests) != 16 or not hasattr(choose_cmd, "bg_slot") or
                    not hasattr(choose_cmd, "ba_slots")):
                raise ValueError("M24 address retiming requires a 16-bank chooser exposing BG/BA pointers")
            # Requests are BA-major/BG-minor. Capture the exact chooser
            # pointer, independent of valid/ready, alongside the source
            # addresses so invalid-cycle address values are preserved too.
            bank_selects = [
                (choose_cmd.bg_slot == bg) & (choose_cmd.ba_slots[bg] == ba)
                for ba in range(4) for bg in range(4)]
            bank_events = {name: [] for name in
                ("valid", "cas", "ras", "we", "is_cmd", "is_read", "is_write")}
            for request in requests:
                for name in bank_events:
                    helper_name = name + "_unqualified"
                    bank_events[name].append(getattr(request, helper_name,
                        getattr(request, name)))
            bank_wants = {
                "cmds": choose_cmd.want_cmds,
                "reads": choose_cmd.want_reads,
                "writes": choose_cmd.want_writes,
                "activates": choose_cmd.want_activates,
            }
            refresh_events = {
                "cas": refresh_cmd.valid & refresh_cmd.ready & refresh_cmd.cas,
                "ras": refresh_cmd.valid & refresh_cmd.ready & refresh_cmd.ras,
                "we": refresh_cmd.valid & refresh_cmd.ready & refresh_cmd.we,
                # The legacy refresh endpoint never assigns is_read/is_write;
                # the resetless endpoint fields therefore remain zero.
                "read": 0,
                "write": 0,
            }
            steerer = _Steerer(commands, dfi, bank_requests=requests,
                bank_selects=bank_selects, bank_events=bank_events,
                refresh_events=refresh_events, bank_wants=bank_wants)
        else:
            steerer = _Steerer(commands, dfi)
        self.submodules += steerer

        # Fixed-slot chooser helpers already include raw request valid and
        # the local class/permission filter. Avoid independently muxing
        # cmd.valid back into the global timing event for the same command.
        slot_helpers = all(hasattr(choose_cmd, name) for name in
            ["_selected_is_activate", "_selected_is_read", "_selected_is_write"])
        def accepted_kind(chooser, kind):
            valid = chooser.cmd.ready if slot_helpers else chooser.accept()
            return valid & getattr(chooser, kind)()

        # tRRD timing (Row to Row delay) -----------------------------------------------------------
        self.submodules.trrdcon = trrdcon = tXXDController(settings.timing.tRRD)
        self.comb += trrdcon.valid.eq(accepted_kind(choose_cmd, "activate"))

        # tFAW timing (Four Activate Window) -------------------------------------------------------
        self.submodules.tfawcon = tfawcon = tFAWController(settings.timing.tFAW)
        self.comb += tfawcon.valid.eq(accepted_kind(choose_cmd, "activate"))

        # RAS control ------------------------------------------------------------------------------
        # Permission can fall only after an accepted ACT. Clear it at that
        # same edge, and re-arm from registered timing-ready signals. The
        # additional recovery cycle is conservative for tRRD and tFAW.
        # Keep the original permission cycle exactly: both operands capture
        # the original register's D inputs, then combine after the edge.
        # This removes accepted-ACT logic from the timer-ready rearm cone.
        self.comb += [
            ras_permission.timer_ready.eq(trrdcon.ready & tfawcon.ready),
            ras_permission.accepted_act.eq(accepted_kind(choose_cmd, "activate")),
        ]

        # tCCD timing (Column to Column delay) -----------------------------------------------------
        self.submodules.tccdcon = tccdcon = tXXDController(
            None if fullrate_bg else settings.timing.tCCD)
        self.comb += tccdcon.valid.eq((accepted_kind(choose_req, "write") | accepted_kind(choose_req, "read")))
        if fullrate_bg:
            # CK/4 controller cycles: tCCD_S=4 CK is one cycle; tCCD_L=8 CK
            # is two cycles. The global controller above admits one CAS per
            # cycle, while this one-cycle cooldown blocks same-BG CAS at t+1.
            self.comb += [
                bg_cooldown.cas.eq((accepted_kind(choose_req, "write") | accepted_kind(choose_req, "read"))),
                bg_cooldown.bank.eq(choose_req.cmd.ba)
            ]

        # CAS control ------------------------------------------------------------------------------
        # Exactly one physical CAS can be steered per CK/4 frame. This
        # structurally enforces tCCD_S=4 CK; fixed BG slots enforce at least
        # 16 CK between same-BG commands (tCCD_L=8 CK). A one-cycle timer adds
        # only reset-state gating and a selected-command fanout path.
        self.comb += cas_allowed.eq(1 if fullrate_bg else tccdcon.ready)

        # tWTR timing (Write to Read delay) --------------------------------------------------------
        write_latency = math.ceil(settings.phy.cwl / settings.phy.nphases)
        self.submodules.twtrcon = twtrcon = tXXDController(
            settings.timing.tWTR + write_latency +
            # tCCD must be added since tWTR begins after the transfer is complete
            settings.timing.tCCD if settings.timing.tCCD is not None else 0)
        self.comb += twtrcon.valid.eq(accepted_kind(choose_req, "write"))

        # Read/write turnaround --------------------------------------------------------------------
        read_available = Signal(reset=0)
        write_available = Signal(reset=0)
        read_available.attr.add("no_retiming")
        write_available.attr.add("no_retiming")
        reads = [req.valid & req.is_read for req in requests]
        writes = [req.valid & req.is_write for req in requests]
        # Register the global request reductions before they drive mode FSM
        # transitions. This adds at most one conservative idle/hold cycle on
        # a direction switch; command selection, acceptance, and DFI steering
        # remain in their original cycle and latency.
        self.sync += [
            read_available.eq(reduce(or_, reads)),
            write_available.eq(reduce(or_, writes)),
        ]

        # Anti Starvation --------------------------------------------------------------------------

        def anti_starvation(timeout):
            en = Signal()
            max_time = Signal()
            if timeout:
                t = timeout - 1
                time = Signal(max=t+1)
                self.comb += max_time.eq(time == 0)
                self.sync += If(~en,
                        time.eq(t)
                    ).Elif(~max_time,
                        time.eq(time - 1)
                    )
            else:
                self.comb += max_time.eq(0)
            return en, max_time

        read_time_en,   max_read_time = anti_starvation(settings.read_time)
        write_time_en, max_write_time = anti_starvation(settings.write_time)

        # Refresh ----------------------------------------------------------------------------------
        self.comb += [bm.refresh_req.eq(refresher.cmd.valid) for bm in bank_machines]
        go_to_refresh = Signal()
        bm_refresh_gnts = [bm.refresh_gnt for bm in bank_machines]
        # Once refresh is requested, banks stop CAS and their grants remain
        # asserted until refresh completes. Delay the rendezvous by one cycle
        # and retain live request gating so stale grants cannot re-enter REFRESH.
        registered_refresh_gnts = Signal()
        registered_refresh_gnts.attr.add("no_retiming")
        self.sync += registered_refresh_gnts.eq(reduce(and_, bm_refresh_gnts))
        self.comb += go_to_refresh.eq(refresher.cmd.valid & registered_refresh_gnts)

        # Datapath ---------------------------------------------------------------------------------
        interface.rdata_clear_request=Signal()
        interface.clear_accept=Signal()
        clear_seen=Signal()
        self.sync += clear_seen.eq(interface.rdata_clear_request)
        clear_edge=interface.rdata_clear_request & ~clear_seen
        interface.return_idle=Signal()
        interface.rdata_start=Signal()
        interface.rdata_due=Signal()
        interface.rdata_due_lookahead=Signal()
        for attribute in ('preserve','dont_merge','dont_retime'):interface.rdata_due.attr.add(attribute)
        interface.return_fault=Signal()
        # Preserve the native fixed qualifier, but retain each fresh physical
        # frame until that qualifier consumes its CAS-ordered payload. Direct
        # bypass supports a frame arriving on the exact qualification cycle.
        pending=Signal(7);count=Signal(5)
        wrptr=Signal(4);rdptr=Signal(4)
        payload=[Signal(256,reset_less=True,name=f'fresh_return_queue_{i}') for i in range(16)]
        raw_fresh=reduce(or_,[p.rddata_valid for p in dfi.phases])
        fresh=Signal(name='fresh_return_stage_valid')
        incoming_data=Signal(256,reset_less=True,name='fresh_return_stage_data')
        fresh.attr.add('preserve');fresh.attr.add('no_retiming')
        incoming_data.attr.add('preserve');incoming_data.attr.add('no_retiming')
        self.sync += [incoming_data.eq(Cat(*[p.rddata for p in dfi.phases])),
            If(interface.clear_accept,fresh.eq(0)).Else(fresh.eq(raw_fresh))]
        # Registered frame arrives long after its CAS debt was registered.
        # No deep current-CAS/FSM cone participates in wide payload capture.
        push=fresh & (pending>count)
        pop=interface.rdata_due
        # CAS scheduling may include deep bank/refresh combinational cones.
        # Register this narrow debt event; raw return cannot arrive before16
        # clocks under the declared profile. Clear also sees the staged debt.
        start_debt=Signal(name='fresh_start_debt')
        for attribute in ('preserve','dont_merge','dont_retime'):start_debt.attr.add(attribute)
        self.sync += If(interface.clear_accept,start_debt.eq(0)).Else(start_debt.eq(interface.rdata_start))
        local_pop=[]
        for slot in range(16):
            chunks=[]
            for chunk in range(8):
                event=Signal(name=f'fresh_pop_local_{slot}_{chunk}')
                for attribute in ('preserve','dont_merge','dont_retime'):event.attr.add(attribute)
                self.sync += event.eq(interface.rdata_due_lookahead)
                chunks.append(event)
            local_pop.append(chunks)
        for slot,word in enumerate(payload):
            # Constant destination and local decode avoids dynamic Array
            # feedback muxes. Idle clear implies pending=count=0/push=0.
            for chunk in range(8):
                sl=slice(chunk*32,(chunk+1)*32)
                self.sync += If(push & ((count<16)|local_pop[slot][chunk]) & (wrptr==slot),word[sl].eq(incoming_data[sl]))
        # Precompute current and next queue heads in parallel. Advancing
        # the pointer selects next-head on the very same native pop edge,
        # avoiding16way asynchronous payload read into external return RAM.
        head_parts=[]
        nonempty_parts=[]
        for chunk in range(8):
            local_ptr=Signal(4,name=f'fresh_head_ptr_{chunk}')
            advanced=Signal(name=f'fresh_head_advanced_{chunk}')
            for control in (local_ptr,advanced):
                for attribute in ('preserve','dont_merge','no_retiming'):control.attr.add(attribute)
            next_ptr=Signal(4);self.comb += next_ptr.eq(local_ptr+1)
            head=Signal(32,reset_less=True,name=f'fresh_head_current_{chunk}')
            following=Signal(32,reset_less=True,name=f'fresh_head_next_{chunk}')
            for datareg in (head,following):
                datareg.attr.add('preserve');datareg.attr.add('no_retiming')
            sl=slice(chunk*32,(chunk+1)*32)
            words=Array([word[sl] for word in payload])
            writing=push & ((count<16)|pop)
            self.sync += [
                head.eq(Mux(writing & (wrptr==local_ptr),incoming_data[sl],words[local_ptr])),
                following.eq(Mux(writing & (wrptr==next_ptr),incoming_data[sl],words[next_ptr])),
                If(interface.clear_accept,local_ptr.eq(0),advanced.eq(0)).Else(
                    advanced.eq(pop & (count!=0)),
                    If(pop & (count!=0),local_ptr.eq(local_ptr+1)))]
            head_parts.append(Mux(advanced,following,head))
            nonempty=Signal(name=f'fresh_nonempty_local_{chunk}')
            for attribute in ('preserve','dont_merge','dont_retime'):nonempty.attr.add(attribute)
            # Exact next count!=0, including empty simultaneous bypass and
            # logical-full overflow behavior. No data qualification changes.
            self.sync += If(interface.clear_accept,nonempty.eq(0)).Else(
                If(push & ~pop,If(count<16,nonempty.eq(1)))
                .Elif(pop & ~push,If(count!=0,nonempty.eq(count!=1))))
            nonempty_parts.append(nonempty)
        self.comb += [interface.rdata.eq(Cat(*[Mux(nonempty_parts[chunk],head_parts[chunk],incoming_data[chunk*32:(chunk+1)*32]) for chunk in range(8)])),
            interface.return_idle.eq((pending==0)&(count==0)&~pop&~interface.rdata_start&~start_debt),
            interface.clear_accept.eq(clear_edge & interface.return_idle)]
        self.sync += [If(clear_edge & ~interface.return_idle,interface.return_fault.eq(1)),
            If(fresh & (pending!=0) & ~push,interface.return_fault.eq(1))]
        # clear_accept implies count==0 and no pop/start debt. Clearing
        # count again is redundant and feeds its own reduction into ENA.
        self.sync += If(push & ~pop,
            If(count!=16,count.eq(count+1))
        ).Elif(pop & ~push,
            If(count!=0,count.eq(count-1))
        )
        self.sync += If(interface.clear_accept,pending.eq(0),wrptr.eq(0),rdptr.eq(0),interface.return_fault.eq(0)).Else(
            If(push & ~pop,
                If(count==16,interface.return_fault.eq(1))
                .Else(wrptr.eq(wrptr+1))
            ).Elif(pop & ~push,
                If(count==0,interface.return_fault.eq(1))
                .Else(rdptr.eq(rdptr+1))
            ).Elif(push & pop,
                If(count!=0,wrptr.eq(wrptr+1),rdptr.eq(rdptr+1))
            ),
            If(start_debt & ~pop,
                If(pending==127,interface.return_fault.eq(1)).Else(pending.eq(pending+1)))
            .Elif(pop & ~start_debt,
                If(pending==0,interface.return_fault.eq(1)).Else(pending.eq(pending-1)))
        )
        all_rddata = [p.rddata for p in dfi.phases]
        all_wrdata = [p.wrdata for p in dfi.phases]
        all_wrdata_mask = [p.wrdata_mask for p in dfi.phases]
        self.comb += [
            Cat(*all_wrdata).eq(interface.wdata),
            Cat(*all_wrdata_mask).eq(0)
        ]

        def steerer_sel(steerer, access):
            assert access in ["read", "write"]
            r = []
            command_phase = Mux(choose_cmd.cmd.pending_write, wrphase, rdphase)
            for i in range(nphases):
                r.append(steerer.sel[i].eq(STEER_NOP))
                r.append(If(command_phase == i, steerer.sel[i].eq(STEER_REQ)))
            return r

        # Control FSM ------------------------------------------------------------------------------
        self.submodules.fsm = fsm = FSM()
        fsm.act("READ",
            read_time_en.eq(1),
            choose_req.want_reads.eq(1),
            choose_cmd.cmd.ready.eq(Mux(choose_cmd.cmd.is_cmd, 1, cas_allowed)),
            steerer_sel(steerer, access="read"),
            If(write_available,
                # TODO: switch only after several cycles of ~read_available?
                If(~read_available | max_read_time,
                    NextState("RTW")
                )
            ),
            If(go_to_refresh,
                NextState("REFRESH")
            )
        )
        fsm.act("WRITE",
            write_time_en.eq(1),
            choose_req.want_writes.eq(1),
            choose_cmd.cmd.ready.eq(Mux(choose_cmd.cmd.is_cmd, 1, cas_allowed)),
            steerer_sel(steerer, access="write"),
            If(read_available,
                If(~write_available | max_write_time,
                    NextState("WTR")
                )
            ),
            If(go_to_refresh,
                NextState("REFRESH")
            )
        )
        fsm.act("REFRESH",
            steerer.sel[0].eq(STEER_REFRESH),
            refresher.cmd.ready.eq(1),
            If(refresh_cmd.last,
                NextState("READ")
            )
        )
        fsm.act("WTR",
            If(twtrcon.ready,
                NextState("READ")
            )
        )
        # TODO: reduce this, actual limit is around (cl+1)/nphases
        fsm.delayed_enter("RTW", "WRITE", settings.phy.read_latency-1)

        if settings.with_bandwidth:
            data_width = settings.phy.dfi_databits*settings.phy.nphases
            self.submodules.bandwidth = Bandwidth(self.choose_req.cmd, data_width)
