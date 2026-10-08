# SPDX-License-Identifier: BSD-2-Clause
"""Two-slot CK-aware DDR4 bank-group CAS scheduler prototype.

Requests are already bank-machine-ready CAS operations. The module chooses
between two requesters and can accept both in one slow-clock cycle when their
commands target opposite DDR4 bank groups.
"""
from migen import Array, If, Module, Mux, Signal


class DualSlotScheduler(Module):
    """Schedule up to two CAS requests in a slow cycle.

    Slot 0 is emitted at CK offset 0 and slot 1 at offset 4. The slots model
    tCCD_S=4 CK; per-group cooldown state preserves tCCD_L=8 CK. Inputs are
    stable ready/valid channels. A command is consumed only when its matching
    input ready is asserted.

    This module does not generate ACT/PRE commands, data-latency enables,
    refresh arbitration, or read/write turnaround. Its caller must supply
    only bank-machine-ready requests in the current read/write direction and
    suppress both slots while refresh or a turnaround blocks CAS traffic.
    """
    def __init__(self, *, rowbits=13, colbits=10,
                 databits=256, maskbits=None, tagbits=16):
        if maskbits is None:
            maskbits = databits // 8
        assert databits > 0 and databits % 8 == 0
        assert maskbits == databits // 8

        self.req_valid = [Signal(name=f"req{i}_valid") for i in range(2)]
        self.req_ready = [Signal(name=f"req{i}_ready") for i in range(2)]
        self.req_group = [Signal(name=f"req{i}_group") for i in range(2)]
        self.req_bank = [Signal(2, name=f"req{i}_bank") for i in range(2)]
        self.req_row = [Signal(rowbits, name=f"req{i}_row") for i in range(2)]
        self.req_col = [Signal(colbits, name=f"req{i}_col") for i in range(2)]
        self.req_write = [Signal(name=f"req{i}_write") for i in range(2)]
        self.req_data = [Signal(databits, name=f"req{i}_data") for i in range(2)]
        self.req_mask = [Signal(maskbits, name=f"req{i}_mask") for i in range(2)]
        self.req_tag = [Signal(tagbits, name=f"req{i}_tag") for i in range(2)]

        self.slot_valid = [Signal(name=f"slot{i}_valid") for i in range(2)]
        self.issue_enable = Signal(reset=1, name="issue_enable")
        self.slot_ck_offset = [Signal(3, name=f"slot{i}_ck_offset") for i in range(2)]
        self.slot_group = [Signal(name=f"slot{i}_group") for i in range(2)]
        self.slot_bank = [Signal(2, name=f"slot{i}_bank") for i in range(2)]
        self.slot_row = [Signal(rowbits, name=f"slot{i}_row") for i in range(2)]
        self.slot_col = [Signal(colbits, name=f"slot{i}_col") for i in range(2)]
        self.slot_write = [Signal(name=f"slot{i}_write") for i in range(2)]
        self.slot_data = [Signal(databits, name=f"slot{i}_data") for i in range(2)]
        self.slot_mask = [Signal(maskbits, name=f"slot{i}_mask") for i in range(2)]
        self.slot_tag = [Signal(tagbits, name=f"slot{i}_tag") for i in range(2)]

        # Remaining CK at the current slow-cycle boundary before this group
        # can accept another CAS. Slot 0 at CK0 leaves zero at the next
        # boundary CK8; slot 1 at CK4 leaves four CK at that boundary.
        cooldown = [Signal(4, reset=0, name=f"group{g}_cooldown_ck")
                    for g in range(2)]
        rr = Signal(reset=0, name="round_robin_priority")

        cooldown_by_group = Array(cooldown)
        eligible = [
            (self.issue_enable & self.req_valid[i] &
             (cooldown_by_group[self.req_group[i]] == 0))
            for i in range(2)
        ]
        first0 = eligible[0] & (~eligible[1] | (rr == 0))
        first1 = eligible[1] & (~eligible[0] | (rr == 1))
        any_first = first0 | first1

        other_group = Mux(first0, self.req_group[1], self.req_group[0])
        other_cooldown = cooldown_by_group[other_group]
        both_valid_opposite = (self.req_valid[0] & self.req_valid[1] &
                               (self.req_group[0] != self.req_group[1]) &
                               (self.req_write[0] == self.req_write[1]))
        # For a second command at CK+4, tCCD_L is legal if its group cooldown
        # has at most four CK left at the current cycle boundary.
        issue_second = any_first & both_valid_opposite & (other_cooldown <= 4)

        self.comb += [
            self.slot_valid[0].eq(any_first),
            self.slot_valid[1].eq(issue_second),
            self.slot_ck_offset[0].eq(0),
            self.slot_ck_offset[1].eq(4),
            self.req_ready[0].eq(first0 | (issue_second & first1)),
            self.req_ready[1].eq(first1 | (issue_second & first0)),
        ]

        for slot, select0 in ((0, first0), (1, first1)):
            # Slot 1 always carries the request not selected for slot 0.
            if slot == 1:
                select0 = first1
            self.comb += [
                self.slot_group[slot].eq(Mux(select0, self.req_group[0],
                                             self.req_group[1])),
                self.slot_bank[slot].eq(Mux(select0, self.req_bank[0],
                                            self.req_bank[1])),
                self.slot_row[slot].eq(Mux(select0, self.req_row[0],
                                          self.req_row[1])),
                self.slot_col[slot].eq(Mux(select0, self.req_col[0],
                                          self.req_col[1])),
                self.slot_write[slot].eq(Mux(select0, self.req_write[0],
                                             self.req_write[1])),
                self.slot_data[slot].eq(Mux(select0, self.req_data[0],
                                            self.req_data[1])),
                self.slot_mask[slot].eq(Mux(select0, self.req_mask[0],
                                            self.req_mask[1])),
                self.slot_tag[slot].eq(Mux(select0, self.req_tag[0],
                                           self.req_tag[1])),
            ]

        # Round-robin only needs to advance after a single grant. A dual grant
        # consumes both requesters, so retaining priority is fair and stable.
        self.sync += If(any_first & ~issue_second,
            rr.eq(first0)
        )

        for group in range(2):
            decayed = Mux(cooldown[group] > 8, cooldown[group] - 8, 0)
            next_cooldown = Signal(4, name=f"group{group}_next_cooldown_ck")
            self.comb += next_cooldown.eq(decayed)
            self.comb += If(self.slot_valid[0] & (self.slot_group[0] == group),
                next_cooldown.eq(0)
            ).Elif(self.slot_valid[1] & (self.slot_group[1] == group),
                next_cooldown.eq(4)
            )
            self.sync += cooldown[group].eq(next_cooldown)

