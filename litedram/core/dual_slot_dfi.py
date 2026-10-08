# SPDX-License-Identifier: BSD-2-Clause
"""Map dual-slot x32 DDR4 BL8 requests onto eight-phase x64 DFI."""
from functools import reduce
from operator import and_

from migen import Cat, Module, Mux, Signal


class DualSlotDFI(Module):
    """Drive two independent x32 BL8 transfers on an 8-phase, x64 DFI.

    Scheduler slot 0 owns phases 0..3 and slot 1 owns phases 4..7. Each
    four-phase region carries one 256-bit payload. rdphase/wrphase select the
    per-slot data-enable phase; the CAS command is placed one phase earlier,
    modulo four, matching LiteDRAM's command/data phase convention.

    Read results are exposed as one 256-bit value per slot, concatenated from
    the slot's four rddata phases. read_valid requires all four data phases to
    be valid. The caller associates response tags and handles bank activation,
    precharge, refresh, read/write turnaround, and other timing management.
    """
    def __init__(self, scheduler, dfi, *, rdphase, wrphase):
        if len(dfi.phases) != 8:
            raise ValueError("dual-slot DFI requires eight logical phases")
        if len(dfi.p0.wrdata) != 64:
            raise ValueError("dual-slot DFI requires 64-bit phases for x32 DDR4")
        if len(dfi.p0.bank) != 3 or len(dfi.p0.cs_n) != 1:
            raise ValueError("dual-slot DFI requires x32 DDR4 bank bits and one rank")
        if not 0 <= rdphase < 4 or not 0 <= wrphase < 4:
            raise ValueError("rdphase/wrphase must be per-slot offsets in 0..3")
        if len(scheduler.slot_data[0]) != 256:
            raise ValueError("each scheduler slot must carry one 256-bit BL8")
        if len(scheduler.slot_mask[0]) != 32:
            raise ValueError("each scheduler slot must carry 32 byte masks")

        self.slot_rdata = [Signal(256, name=f"slot{i}_rdata") for i in range(2)]
        self.slot_read_valid = [Signal(name=f"slot{i}_read_valid") for i in range(2)]

        rd_cmd_offset = (rdphase - 1) % 4
        wr_cmd_offset = (wrphase - 1) % 4
        for phase_index, phase in enumerate(dfi.phases):
            slot = phase_index // 4
            offset = phase_index % 4
            req_valid = scheduler.slot_valid[slot]
            req_write = scheduler.slot_write[slot]
            req_read = ~req_write
            write_command = req_valid & req_write & (offset == wr_cmd_offset)
            read_command = req_valid & req_read & (offset == rd_cmd_offset)
            command = write_command | read_command
            write_enable = req_valid & req_write & (offset == wrphase)
            read_enable = req_valid & req_read & (offset == rdphase)

            self.comb += [
                phase.cs_n.eq(~command),
                phase.address.eq(Mux(command, scheduler.slot_col[slot], 0)),
                phase.bank.eq(Mux(command, Cat(scheduler.slot_bank[slot],
                                               scheduler.slot_group[slot]), 0)),
                phase.cas_n.eq(~command),
                phase.ras_n.eq(1),
                phase.we_n.eq(~write_command),
                phase.cke.eq(1),
                phase.odt.eq(1),
                phase.reset_n.eq(1),
                phase.act_n.eq(1),
                phase.wrdata_en.eq(write_enable),
                phase.rddata_en.eq(read_enable),
                phase.wrdata.eq(Mux(
                    req_valid & req_write,
                    scheduler.slot_data[slot][offset*64:(offset+1)*64], 0)),
                phase.wrdata_mask.eq(Mux(
                    req_valid & req_write,
                    scheduler.slot_mask[slot][offset*8:(offset+1)*8], 0)),
            ]

        for slot in range(2):
            burst_phases = dfi.phases[4*slot:4*slot+4]
            self.comb += [
                self.slot_rdata[slot].eq(Cat(*[p.rddata for p in burst_phases])),
                self.slot_read_valid[slot].eq(
                    reduce(and_, [p.rddata_valid for p in burst_phases])),
            ]
