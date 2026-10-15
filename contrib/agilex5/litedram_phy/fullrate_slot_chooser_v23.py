"""V23: V14 plus exact selected DFI command-field validity helpers."""
from migen import *
from litex.soc.interconnect import stream
from litedram.common import cmd_request_rw_layout


class SlotCommandChooserV23(Module):
    """Select one ready bank from the current free-running BG slot.

    ``requests`` must be ordered ``[bank 0, 4, 8, 12, 1, 5, ...]``: the
    low two request-index bits identify BG and the upper two identify BA.
    The BG slot advances every controller cycle independently of command
    acceptance. Each BG keeps a registered BA choice and repairs it from the
    other local raw-valid requests when that choice is no longer valid.
    """
    def __init__(self, requests, eligible=None, bank_group=False):
        del eligible  # BG slots guarantee the same-group cooldown.
        if len(requests) != 16:
            raise ValueError("SlotCommandChooserV23 requires 16 bank requests")
        self.want_reads = Signal()
        self.want_writes = Signal()
        self.want_cmds = Signal()
        self.want_activates = Signal()
        self.monitor_clear = Signal()
        self.accepted_command_q = Signal(16)
        self.accepted_read_q = Signal(16)
        self.accepted_write_q = Signal(16)
        self.accepted_command_q.attr.add("preserve")
        self.accepted_command_q.attr.add("no_retiming")
        self.accepted_read_q.attr.add("preserve")
        self.accepted_read_q.attr.add("no_retiming")
        self.accepted_write_q.attr.add("preserve")
        self.accepted_write_q.attr.add("no_retiming")

        addressbits = len(requests[0].a)
        bankbits = len(requests[0].ba)
        self.cmd = cmd = stream.Endpoint(cmd_request_rw_layout(addressbits, bankbits))
        cmd.pending_write = Signal()

        # Classify commands independently.  The equality-based V1 filtering
        # treated disabled read/write classes as implicit command selection;
        # V2 selects the command class explicitly so CMD and REQ choosers can
        # run without a global ready cross-gate.
        valids = []
        class_permissions = []
        activate_valids = []
        read_valids = []
        write_valids = []
        cas_cmd_terms, cas_read_terms, cas_write_terms = [], [], []
        ras_cmd_nonact_terms, ras_act_terms = [], []
        ras_read_terms, ras_write_terms = [], []
        we_cmd_terms, we_read_terms, we_write_terms = [], [], []
        for i, request in enumerate(requests):
            is_activate = request.ras & ~request.cas & ~request.we
            command = request.is_cmd & self.want_cmds & (
                ~is_activate | self.want_activates)
            read = request.is_read & self.want_reads
            write = request.is_write & self.want_writes
            valid = Signal(name="slot_request_valid_{}".format(i))
            class_permission = command | read | write
            valid_expression = request.valid & class_permission
            self.comb += valid.eq(valid_expression)
            valids.append(valid)
            class_permissions.append(class_permission)
            # Keep global ACT permission out of the per-bank select terms.
            # The single selected raw ACT is gated after local selection below.
            activate_valids.append(request.valid & request.is_cmd & is_activate)
            read_valids.append(request.valid & read)
            write_valids.append(request.valid & write)
            for field, cmd_terms, read_terms, write_terms in (
                    (request.cas, cas_cmd_terms, cas_read_terms, cas_write_terms),
                    (request.we,  we_cmd_terms,  we_read_terms,  we_write_terms)):
                cmd_terms.append(request.valid & request.is_cmd & field)
                read_terms.append(request.valid & request.is_read & field)
                write_terms.append(request.valid & request.is_write & field)
            ras_cmd_nonact_terms.append(request.valid & request.is_cmd & request.ras & ~is_activate)
            ras_act_terms.append(request.valid & request.is_cmd & request.ras & is_activate)
            ras_read_terms.append(request.valid & request.is_read & request.ras)
            ras_write_terms.append(request.valid & request.is_write & request.ras)

        # Keep the diagnostic event producer local to each bank. The OR of
        # this vector is the selected command acceptance from the prior cycle.
        self.sync += [If(self.monitor_clear,
                self.accepted_command_q[i].eq(0)
            ).Else(
                self.accepted_command_q[i].eq(request.valid & request.ready & request.is_cmd)
            ) for i, request in enumerate(requests)]
        self.sync += [If(self.monitor_clear,
                self.accepted_read_q[i].eq(0), self.accepted_write_q[i].eq(0)
            ).Else(
                self.accepted_read_q[i].eq(request.valid & request.ready & request.is_read),
                self.accepted_write_q[i].eq(request.valid & request.ready & request.is_write)
            ) for i, request in enumerate(requests)]

        bg_slot = Signal(2, reset=0, name="slot_bg")
        bg_slot.attr.add("no_retiming")
        bg_slot_onehot = Signal(4, reset=1, name="slot_bg_onehot")
        bg_slot_onehot.attr.add("no_retiming")
        registered_valids = [Signal(reset=0, name="slot_registered_raw_valid_{}".format(i))
            for i in range(16)]
        self.registered_valids = registered_valids
        self.sync += [registered_valid.eq(request.valid)
            for registered_valid, request in zip(registered_valids, requests)]
        self.sync += [
            bg_slot.eq(bg_slot + 1),
            bg_slot_onehot.eq(Cat(bg_slot_onehot[3], bg_slot_onehot[:3])),
        ]

        # Each group independently remembers its selected BA. If that bank
        # stops being eligible, select the first eligible local bank and
        # register it. This is a four-entry local choice, not a global grant.
        ba_slots = []
        for bg in range(4):
            ba = Signal(2, reset=0, name="slot_ba_bg{}".format(bg))
            ba.attr.add("no_retiming")
            ba_slots.append(ba)
            local_valid = [registered_valids[ba_index*4 + bg] for ba_index in range(4)]
            pointer_valid = Array(local_valid)[ba]
            any_valid = local_valid[0] | local_valid[1] | local_valid[2] | local_valid[3]
            fallback = Signal(2, name="slot_fallback_bg{}".format(bg))
            self.comb += fallback.eq(Mux(local_valid[0], 0,
                Mux(local_valid[1], 1, Mux(local_valid[2], 2, 3))))
            self.sync += If(~pointer_valid & any_valid, ba.eq(fallback))

        # Public read-only pointer taps let M24 capture the exact one-hot
        # source selection beside its per-bank address snapshots. They add no
        # logic to this chooser and preserve V16 arbitration behavior.
        self.bg_slot = bg_slot
        self.ba_slots = ba_slots

        # First select among the four BAs local to each BG, then select one
        # BG. This avoids building a global 16-way payload and type mux.
        def hierarchical_select(values):
            per_bg = []
            for bg in range(4):
                per_bg.append(Array([values[ba * 4 + bg] for ba in range(4)])[
                    ba_slots[bg]])
            return Array(per_bg)[bg_slot]

        selected_valid = hierarchical_select(valids)
        for name in ("a", "ba", "is_read", "is_write", "is_cmd"):
            self.comb += getattr(cmd, name).eq(hierarchical_select(
                [getattr(request, name) for request in requests]))
        self.comb += [
            cmd.pending_write.eq(hierarchical_select(
                [request.pending_write for request in requests])),
            cmd.valid.eq(selected_valid),
        ]
        for name in ("cas", "ras", "we"):
            self.comb += getattr(cmd, name).eq(0)
            self.comb += If(cmd.valid,
                getattr(cmd, name).eq(hierarchical_select(
                    [getattr(request, name) for request in requests])))

        # Ready is local to each bank and depends only on selected command
        # class permissions. It may be asserted while request.valid is low;
        # the actual accepted command remains request.valid & request.ready.
        # Keeping raw valid out of ready breaks the tRAS-ready feedback path.
        for index, request in enumerate(requests):
            bg = index % 4
            ba = index // 4
            self.comb += If(bg_slot_onehot[bg] & (ba_slots[bg] == ba) &
                class_permissions[index] & cmd.ready,
                request.ready.eq(1))
            # A bank uses these only in its mutually exclusive read/write
            # branch. Keep ACT permission and command-class decode out of
            # the CAS buffer-capture and recovery-timer paths.
            if hasattr(request, "ready_read"):
                assert hasattr(request, "ready_write")
                selected = bg_slot_onehot[bg] & (ba_slots[bg] == ba) & cmd.ready
                self.comb += [request.ready_read.eq(selected & self.want_reads),
                    request.ready_write.eq(selected & self.want_writes)]
            if hasattr(request, "ready_activate"):
                assert hasattr(request, "ready_precharge")
                selected = bg_slot_onehot[bg] & (ba_slots[bg] == ba) & cmd.ready
                self.comb += [
                    request.ready_activate.eq(selected & self.want_cmds & self.want_activates),
                    request.ready_precharge.eq(selected & self.want_cmds),
                ]

        self._selected_is_activate = Signal()
        self._selected_is_read = Signal()
        self._selected_is_write = Signal()
        self.comb += [
            self._selected_is_activate.eq(hierarchical_select(activate_valids) &
                self.want_cmds & self.want_activates),
            self._selected_is_read.eq(hierarchical_select(read_valids)),
            self._selected_is_write.eq(hierarchical_select(write_valids)),
        ]
        # Exact DFI field-valid terms let the steerer avoid re-decoding the
        # selected endpoint class. Gate class permission only after the same
        # hierarchical request selection used for the command fields.
        self._selected_cas_valid = Signal()
        self._selected_we_valid = Signal()
        self._selected_ras_valid = Signal()
        for field_valid, cmd_terms, read_terms, write_terms in (
                (self._selected_cas_valid, cas_cmd_terms, cas_read_terms, cas_write_terms),
                (self._selected_we_valid, we_cmd_terms, we_read_terms, we_write_terms)):
            self.comb += field_valid.eq(
                (hierarchical_select(cmd_terms) & self.want_cmds) |
                (hierarchical_select(read_terms) & self.want_reads) |
                (hierarchical_select(write_terms) & self.want_writes))
        self.comb += self._selected_ras_valid.eq(
            (hierarchical_select(ras_cmd_nonact_terms) & self.want_cmds) |
            (hierarchical_select(ras_act_terms) & self.want_cmds & self.want_activates) |
            (hierarchical_select(ras_read_terms) & self.want_reads) |
            (hierarchical_select(ras_write_terms) & self.want_writes))

        # Preserve the ACT bank-group identity through the permission capture.
        # OR of this vector is cycle-equivalent to the old selected ACT term.
        self.activate_by_bg = Signal(4, name="activate_by_bg")
        for bg in range(4):
            local_activate = Array([activate_valids[ba*4 + bg] for ba in range(4)])[ba_slots[bg]]
            self.comb += self.activate_by_bg[bg].eq(bg_slot_onehot[bg] & local_activate &
                self.want_cmds & self.want_activates)

    def accept(self):
        return self.cmd.valid & self.cmd.ready

    def activate(self):
        return self._selected_is_activate

    def write(self):
        return self._selected_is_write

    def read(self):
        return self._selected_is_read


SlotCommandChooserV14 = SlotCommandChooserV23
SlotCommandChooserV13 = SlotCommandChooserV23
SlotCommandChooserV15 = SlotCommandChooserV23
