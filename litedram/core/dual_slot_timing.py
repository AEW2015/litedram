#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Global timing admission for two CAS slots in an 8-CK controller cycle.

The upstream arbiter must accept at most one read/write direction in a cycle,
assigning bank groups 0/1 to fixed slots at CK0/CK4. This fixes same-group
spacing at eight CK and different-group spacing at four CK. Turnaround is
counted from the later command in a pair.
All constructor timing values are physical DDR clock counts (CK).
"""

from migen import If, Module, Mux, Signal


class DualSlotTimingAdmission(Module):
    """Admit CAS requests while enforcing global read/write turnaround.

    ``accepted`` and ``req_write`` describe the two CAS slots selected by an
    upstream arbiter. The per-slot ``read_ready``/``write_ready`` vectors tell
    that arbiter whether each direction is legal at CK0/CK4. ``refresh_inhibit``
    and a valid exclusive maintenance command block both slots.

    The conservative physical-CK turnaround floors are:

    * write to read: max(tCCD, CWL + BL/2 + tWTR), allowing the write burst
      and configured post-burst write-to-read delay to complete;
    * read to write: max(tCCD, CL + BL/2 + 2 - CWL), allowing read data to
      finish before the write data burst begins with a conservative 2-CK PHY
      margin.

    A cooldown set by a command at CK0 is reduced by 8 CK before the next
    controller boundary; a command at CK4 is reduced by 4 CK. This preserves
    the slot timestamp when a pair is accepted.
    """

    def __init__(self, *, cl, cwl, twtr, tccd_s=4, tccd_l=8, bl=8,
                 write_to_read_ck=None, read_to_write_ck=None):
        assert cl > 0 and cwl > 0 and twtr >= 0
        assert tccd_s > 0 and tccd_l >= tccd_s
        if tccd_s != 4 or tccd_l != 8:
            raise ValueError("Fixed slots require tCCD_S=4 and tCCD_L=8 CK")
        assert bl > 0 and bl % 2 == 0
        self.req_write = [Signal(name=f"req{i}_write") for i in range(2)]
        self.accepted = [Signal(name=f"accepted{i}") for i in range(2)]
        self.read_ready = [Signal(name=f"read_ready{i}") for i in range(2)]
        self.write_ready = [Signal(name=f"write_ready{i}") for i in range(2)]
        self.refresh_inhibit = Signal(name="refresh_inhibit")
        self.maintenance_valid = Signal(name="maintenance_valid")
        self.maintenance_ready = Signal(name="maintenance_ready")

        wtr_floor = max(tccd_l, cwl + bl // 2 + twtr,
                        0 if write_to_read_ck is None else write_to_read_ck)
        rtw_floor = max(tccd_l, cl + bl // 2 + 2 - cwl,
                        0 if read_to_write_ck is None else read_to_write_ck)
        assert wtr_floor >= 0 and rtw_floor >= 0
        max_floor = max(wtr_floor, rtw_floor, 8)
        width = max(1, max_floor.bit_length())
        wtr_left = Signal(width, reset=0, name="write_to_read_left_ck")
        rtw_left = Signal(width, reset=0, name="read_to_write_left_ck")

        blocked = self.refresh_inhibit | self.maintenance_valid
        self.comb += [
            self.maintenance_ready.eq(~self.refresh_inhibit),
        ]
        for slot, offset in enumerate((0, 4)):
            self.comb += [
                self.read_ready[slot].eq(~blocked & (wtr_left <= offset)),
                self.write_ready[slot].eq(~blocked & (rtw_left <= offset)),
            ]

        # Cooldowns decay by one full slow cycle. Accepted slot 1 is the later
        # timestamp, so it leaves only 4 CK elapsed by the next boundary.
        wtr_decay = Mux(wtr_left > 8, wtr_left - 8, 0)
        rtw_decay = Mux(rtw_left > 8, rtw_left - 8, 0)
        wtr_reload = Mux(self.accepted[1], max(0, wtr_floor - 4),
                         max(0, wtr_floor - 8))
        rtw_reload = Mux(self.accepted[1], max(0, rtw_floor - 4),
                         max(0, rtw_floor - 8))
        self.sync += [
            wtr_left.eq(wtr_decay),
            rtw_left.eq(rtw_decay),
            If(self.accepted[0] | self.accepted[1],
                If(self.req_write[0] & self.accepted[0],
                    wtr_left.eq(wtr_reload)
                ).Elif(self.req_write[1] & self.accepted[1],
                    wtr_left.eq(wtr_reload)
                ).Else(
                    rtw_left.eq(rtw_reload)
                )
            )
        ]
