#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Arbitrate real LiteDRAM BankMachines for fixed DDR4 dual CAS slots.

The first CAS slot is assigned to bank group 0 at CK offset 0 and the second
to bank group 1 at CK offset 4. ACT and PRE commands share one serialized
command stream. Timing, data ownership, and refresh command issue remain with
the caller.
"""

from migen import *
from migen.genlib.roundrobin import RoundRobin, SP_CE
from functools import reduce
from operator import and_, or_

from litex.soc.interconnect import stream
from litedram.common import cmd_request_rw_layout


class DualSlotBankMachine(Module):
    """Arbitrate any number of real BankMachines into two CAS slots.

    ``bank_machines`` may cover multiple ranks. For the supported x32 device,
    the bank-group bit is BA2. For x64 devices with four groups, BA2 selects
    one of two conservative slot classes (groups 0/2 or 1/3); commands within
    a class retain full tCCD_L spacing.
    ``cas_eligible`` lets the caller remove banks whose owning port cannot
    accept the corresponding data or response. The selected bank index is
    exported for that ownership check and data steering.

    ``cas_cmd[0]`` and ``cas_cmd[1]`` are fixed physical slots: group 0 at CK0
    and group 1 at CK4. Their ``ready`` inputs carry all global timing and
    datapath admission. Opposite slots can handshake together only when their
    directions match. ``cmd`` serializes ACT and PRE requests from every bank.
    """
    def __init__(self, bank_machines, *, addressbits=None, bankbits=None):
        if not bank_machines:
            raise ValueError("at least one BankMachine is required")
        self.bank_machines = list(bank_machines)
        n = len(bank_machines)
        sample = bank_machines[0].cmd
        addressbits = len(sample.a) if addressbits is None else addressbits
        bankbits = len(sample.ba) if bankbits is None else bankbits
        layout = cmd_request_rw_layout(addressbits, bankbits)

        self.cas_cmd = [stream.Endpoint(layout), stream.Endpoint(layout)]
        self.cas_bank = [Signal(max=n, name=f"cas_bank{i}") for i in range(2)]
        self.cas_eligible = Signal(n, reset=(1 << n) - 1,
                                   name="cas_eligible")
        self.read_enable = Signal(name="read_enable")
        self.write_enable = Signal(name="write_enable")
        self.command_enable = Signal(name="command_enable")
        self.activate_enable = Signal(name="activate_enable")
        self.precharge_enable = Signal(name="precharge_enable")
        self.refresh_req = Signal(name="refresh_req")
        self.refresh_gnt = Signal(name="refresh_gnt")
        self.cmd = stream.Endpoint(layout)

        self.comb += [bm.refresh_req.eq(self.refresh_req)
                      for bm in bank_machines]
        self.comb += self.refresh_gnt.eq(reduce(and_,
            [bm.refresh_gnt for bm in bank_machines]))

        # Two independent round-robin arbiters prevent an ineligible bank from
        # occupying a grant and blocking an eligible peer in the same group.
        slot0_read = Signal()
        slot0_write = Signal()
        for group in range(2):
            arb = RoundRobin(n, SP_CE)
            setattr(self.submodules, f"cas_arbiter{group}", arb)
            requests = []
            for i, bm in enumerate(bank_machines):
                c = bm.cmd
                group_match = (c.ba[2] == group) if bankbits > 2 else Constant(0)
                direction = ((c.is_read & self.read_enable) |
                             (c.is_write & self.write_enable))
                same_direction = 1
                if group == 1:
                    # A lone group-1 command may use slot 1. When both slots
                    # are populated, the second command must match slot 0.
                    same_direction = (~self.cas_cmd[0].valid |
                        ((c.is_read == slot0_read) &
                         (c.is_write == slot0_write)))
                requests.append(c.valid & ~c.is_cmd & direction &
                                self.cas_eligible[i] & group_match &
                                same_direction)
            self.comb += arb.request.eq(Cat(*requests))
            grant = Array([Constant(i, bits_for(n)) for i in range(n)])[arb.grant]
            out = self.cas_cmd[group]
            self.comb += [out.valid.eq(Array(requests)[arb.grant]),
                          self.cas_bank[group].eq(grant)]
            for name, _width in layout:
                values = [getattr(bm.cmd, name) for bm in bank_machines]
                self.comb += getattr(out, name).eq(Array(values)[arb.grant])
            if group == 0:
                reads = [bm.cmd.is_read for bm in bank_machines]
                writes = [bm.cmd.is_write for bm in bank_machines]
                self.comb += [slot0_read.eq(Array(reads)[arb.grant]),
                              slot0_write.eq(Array(writes)[arb.grant])]
            for i, bm in enumerate(bank_machines):
                self.comb += If(out.valid & out.ready & (arb.grant == i),
                    bm.cmd.ready.eq(1))
            self.comb += arb.ce.eq(out.ready | ~out.valid)

        # Commands that change row state use one serialized output. Activate
        # and precharge eligibility includes both global timing and each BM's
        # own tRC/tRAS/tWTP checks (which are already reflected in cmd.valid).
        arb = RoundRobin(n, SP_CE)
        self.submodules.command_arbiter = arb
        command_requests = []
        for bm in bank_machines:
            c = bm.cmd
            is_act = c.ras & ~c.cas & ~c.we
            is_pre = c.ras & ~c.cas & c.we
            activate_ready = (bm.activate_ready
                              if hasattr(bm, "activate_ready") else Constant(1))
            allowed = ((is_act & self.activate_enable & activate_ready) |
                       (is_pre & self.precharge_enable))
            command_requests.append(c.valid & c.is_cmd & allowed &
                                    self.command_enable)
        self.comb += arb.request.eq(Cat(*command_requests))
        self.comb += self.cmd.valid.eq(Array(command_requests)[arb.grant])
        for name, _width in layout:
            values = [getattr(bm.cmd, name) for bm in bank_machines]
            self.comb += getattr(self.cmd, name).eq(Array(values)[arb.grant])
        for i, bm in enumerate(bank_machines):
            self.comb += If(self.cmd.valid & self.cmd.ready & (arb.grant == i),
                bm.cmd.ready.eq(1))
        self.comb += arb.ce.eq(self.cmd.ready | ~self.cmd.valid)

