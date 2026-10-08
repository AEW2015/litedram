#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Opt-in, fixed-bank-group DDR4 two-slot controller multiplexer."""

from functools import reduce
from operator import or_

from migen import *
from litex.soc.interconnect.csr import AutoCSR

from litedram.common import cmd_request_layout, tXXDController, tFAWController
from litedram.core.multiplexer import _Steerer, STEER_NOP, STEER_CMD, STEER_REFRESH
from litedram.core.dual_slot_bankmachine import DualSlotBankMachine
from litedram.core.dual_slot_timing import DualSlotTimingAdmission


class DualSlotMultiplexer(Module, AutoCSR):
    def __init__(self, settings, bank_machines, refresher, dfi, interface):
        if settings.phy.nphases != 8 or len(bank_machines) != 2**settings.geom.bankbits:
            raise ValueError("Two-slot multiplexer requires eight DFI phases and every bank")
        for phase in (settings.phy.rdphase, settings.phy.wrphase):
            if isinstance(phase, int):
                if not 0 <= phase < 4:
                    raise ValueError("Read/write phases must be offsets within a four-phase slot")
            elif len(phase) != 2:
                raise ValueError("Programmable read/write phases must be two bits wide")
        if settings.with_bandwidth:
            raise ValueError("Use native DMA counters for two-slot bandwidth measurement")

        self.submodules.arbiter = arbiter = DualSlotBankMachine(
            bank_machines, bankbits=settings.geom.bankbits)
        self.submodules.timing = timing = DualSlotTimingAdmission(
            cl=settings.phy.cl, cwl=settings.phy.cwl,
            twtr=settings.timing.tWTR*8,
            # Round upward from the ordinary slow-clock controller's recovery
            # budget, including the differing read/write command phases.
            write_to_read_ck=(settings.timing.tWTR +
                (settings.phy.cwl + 7)//8 + settings.timing.tCCD + 1)*8,
            read_to_write_ck=settings.phy.read_latency*8)
        self.submodules.trrd = trrd = tXXDController(settings.timing.tRRD)
        self.submodules.tfaw = tfaw = tFAWController(settings.timing.tFAW)
        activate = arbiter.cmd.valid & arbiter.cmd.ready & arbiter.cmd.ras & ~arbiter.cmd.we
        self.comb += [trrd.valid.eq(activate), tfaw.valid.eq(activate),
            arbiter.activate_enable.eq(trrd.ready & tfaw.ready),
            arbiter.precharge_enable.eq(1)]

        # A persistent refresh request stops admission. Bank recovery timers
        # include the later CAS slot and drain reads before granting refresh.
        refresh_req = Signal()
        if settings.with_registered_refresh_request:
            self.sync += refresh_req.eq(refresher.cmd.valid)
        else:
            self.comb += refresh_req.eq(refresher.cmd.valid)
        self.comb += [arbiter.refresh_req.eq(refresh_req),
            timing.refresh_inhibit.eq(refresh_req),
            timing.maintenance_valid.eq(arbiter.cmd.valid)]

        for bank, bm in enumerate(bank_machines):
            # BA2 is BG0 for both supported devices. With four groups this
            # pairs groups 0/2 and 1/3 into conservative tCCD_L slot classes.
            slot = (bank >> 2) & 1
            self.comb += arbiter.cas_eligible[bank].eq(
                (bm.cmd.is_read & interface.bank_read_ready[bank] & timing.read_ready[slot]) |
                (bm.cmd.is_write & interface.bank_write_ready[bank] & timing.write_ready[slot]))
        for slot, command in enumerate(arbiter.cas_cmd):
            accepted = command.valid & command.ready
            self.comb += [
                command.ready.eq(~arbiter.cmd.valid & ~refresh_req),
                timing.accepted[slot].eq(accepted),
                timing.req_write[slot].eq(command.is_write),
                interface.slot_bank[slot].eq(arbiter.cas_bank[slot]),
                interface.slot_read[slot].eq(accepted & command.is_read),
                interface.slot_write[slot].eq(accepted & command.is_write),
            ]

        nop = Record(cmd_request_layout(settings.geom.addressbits, settings.geom.bankbits))
        # Keep the ordinary steerer registration and CAS/data-enable phase
        # convention. A second request input owns phases four through seven.
        commands = [nop, arbiter.cmd, arbiter.cas_cmd[0], refresher.cmd, arbiter.cas_cmd[1]]
        self.submodules.steerer = steerer = _Steerer(commands, dfi)
        reads = reduce(or_, [bm.cmd.valid & bm.cmd.is_read for bm in bank_machines])
        writes = reduce(or_, [bm.cmd.valid & bm.cmd.is_write for bm in bank_machines])
        read_count = Signal(max=max(2, settings.read_time + 1))
        write_count = Signal(max=max(2, settings.write_time + 1))
        self.submodules.fsm = fsm = FSM(reset_state="READ")
        self.sync += [
            If(~fsm.ongoing("READ"), read_count.eq(0)).Elif(read_count < settings.read_time,
                read_count.eq(read_count + 1)),
            If(~fsm.ongoing("WRITE"), write_count.eq(0)).Elif(write_count < settings.write_time,
                write_count.eq(write_count + 1)),
        ]
        # An ACT/PRE already waiting in a BankMachine must retire before that
        # machine can enter REFRESH. Stop CAS immediately, but drain maintenance
        # until every bank grants refresh; otherwise an unlucky request hangs.
        self.comb += [arbiter.command_enable.eq(~fsm.ongoing("REFRESH")),
            arbiter.cmd.ready.eq(~fsm.ongoing("REFRESH"))]

        def selections(phase):
            # Maintenance and CAS admission are exclusive. Preserve phase zero
            # for maintenance even when a programmable CAS phase selects zero.
            selects = [steerer.sel[0].eq(STEER_CMD)]
            for offset in range(4):
                selects.append(If(~arbiter.cmd.valid & (phase == offset),
                    steerer.sel[offset].eq(2),
                    steerer.sel[offset + 4].eq(4)))
            return selects

        refresh_transition = If(refresh_req & arbiter.refresh_gnt, NextState("REFRESH"))
        fsm.act("READ",
            arbiter.read_enable.eq(~refresh_req),
            *selections(settings.phy.rdphase),
            If(writes & (~reads | ((read_count == settings.read_time) if settings.read_time else 0)),
                NextState("WRITE")),
            refresh_transition)
        fsm.act("WRITE",
            arbiter.write_enable.eq(~refresh_req),
            *selections(settings.phy.wrphase),
            If(reads & (~writes | ((write_count == settings.write_time) if settings.write_time else 0)),
                NextState("READ")),
            If(refresh_req & arbiter.refresh_gnt, NextState("REFRESH")))
        fsm.act("REFRESH", steerer.sel[0].eq(STEER_REFRESH), refresher.cmd.ready.eq(1),
            If(refresher.cmd.last, NextState("READ")))

        for slot in range(2):
            phases = dfi.phases[slot*4:slot*4+4]
            wdata = interface.wdata if slot == 0 else interface.slot1_wdata
            mask = interface.wdata_we if slot == 0 else interface.slot1_wdata_we
            rdata = interface.rdata if slot == 0 else interface.slot1_rdata
            self.comb += [Cat(*[p.wrdata for p in phases]).eq(wdata),
                Cat(*[p.wrdata_mask for p in phases]).eq(~mask),
                rdata.eq(Cat(*[p.rddata for p in phases]))]
