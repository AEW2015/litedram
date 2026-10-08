#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Simulation coverage for the all-bank dual-slot BankMachine arbiter."""

import unittest

from migen import *
from litex.gen.sim import run_simulation as litex_run_simulation

from litedram.common import Settings, LiteDRAMInterface
from litedram.core.bankmachine import BankMachine
from litedram.core.dual_slot_bankmachine import DualSlotBankMachine


class DualSlotBankMachineDUT(Module):
    def __init__(self, nbanks=8):
        settings = Settings()
        settings.set_attributes(dict(
            cmd_buffer_depth=4, cmd_buffer_buffered=False,
            with_auto_precharge=False, with_activate_eligibility=True,
            with_registered_timing_valid=False,
        ))
        settings.phy = Settings()
        settings.phy.set_attributes(dict(memtype="DDR4", cwl=4, nphases=4,
            nranks=1, dfi_databits=32))
        settings.geom = Settings()
        settings.geom.set_attributes(dict(bankbits=3, rowbits=8, colbits=10,
            addressbits=10))
        settings.timing = Settings()
        settings.timing.set_attributes(dict(tRAS=2, tRC=8, tRCD=2, tRP=2,
            tWR=2, tCCD=2))
        self.banks = []
        align = 3
        address_width = LiteDRAMInterface(align, settings).address_width
        for bank in range(nbanks):
            bm = BankMachine(bank, address_width, align, 1, settings)
            self.banks.append(bm)
            self.submodules += bm
        self.submodules.arbiter = self.arbiter = DualSlotBankMachine(self.banks)


class TestDualSlotBankMachine(unittest.TestCase):
    def test_all_bank_pair_backpressure_and_refresh(self):
        dut = DualSlotBankMachineDUT(nbanks=8)
        arb = dut.arbiter
        act_banks = []
        retired = []
        refresh_grants = []

        def bench():
            yield arb.command_enable.eq(1)
            yield arb.activate_enable.eq(1)
            yield arb.precharge_enable.eq(1)
            yield arb.cmd.ready.eq(1)
            yield arb.read_enable.eq(1)
            yield arb.cas_eligible.eq((1 << 1) | (1 << 4))
            # Queue three real requests: two peers in BG0 and one in BG1.
            address = (3 << (10 - 3)) | (5 >> 3)
            for bank in (0, 1, 4):
                req = dut.banks[bank].req
                yield req.addr.eq(address)
                yield req.we.eq(0)
                yield req.valid.eq(1)
            yield
            for bank in (0, 1, 4):
                yield dut.banks[bank].req.valid.eq(0)

            # Allow the real BankMachines to issue their ACT commands. Each
            # command is accepted exactly once by the serialized stream.
            for _ in range(20):
                if (yield arb.cmd.valid) and (yield arb.cmd.ready):
                    act_banks.append((yield arb.cmd.ba))
                yield
            self.assertEqual(set(act_banks), {0, 1, 4})

            # Exclude bank 0 at the arbiter input. Bank 1 must be selected
            # from the same group while bank 4 can use its fixed slot 1.
            yield arb.cas_cmd[0].ready.eq(0)
            yield arb.cas_cmd[1].ready.eq(1)
            for _ in range(20):
                if (yield arb.cas_cmd[0].valid):
                    self.assertEqual((yield arb.cas_bank[0]), 1)
                if (yield arb.cas_cmd[1].valid) and (yield arb.cas_cmd[1].ready):
                    retired.append((yield arb.cas_bank[1]))
                yield
            self.assertEqual(retired, [4])

            # Once slot 1 is idle, bank 1 is still the eligible group-0 peer.
            yield arb.cas_cmd[0].ready.eq(1)
            yield arb.cas_cmd[1].ready.eq(0)
            for _ in range(20):
                if (yield arb.cas_cmd[0].valid) and (yield arb.cas_cmd[0].ready):
                    retired.append((yield arb.cas_bank[0]))
                yield
            self.assertEqual(retired, [4, 1])

            # Exclude bank 1; bank 0 becomes visible and retires on handshake.
            yield arb.cas_eligible.eq((1 << 0) | (1 << 4))
            for _ in range(20):
                if (yield arb.cas_cmd[0].valid) and (yield arb.cas_cmd[0].ready):
                    retired.append((yield arb.cas_bank[0]))
                yield
            self.assertEqual(retired, [4, 1, 0])

            # The request is broadcast to every bank and grant waits for the
            # real per-bank refresh state machines.
            yield arb.refresh_req.eq(1)
            for _ in range(20):
                grants = []
                for bm in dut.banks:
                    grants.append((yield bm.refresh_gnt))
                refresh_grants.append(grants)
                if (yield arb.refresh_gnt):
                    break
                yield
            self.assertTrue(refresh_grants)
            self.assertEqual(refresh_grants[-1], [1] * 8)
            for bm in dut.banks:
                self.assertEqual((yield bm.refresh_req), 1)
            yield arb.refresh_req.eq(0)
            yield

        # Command-buffer FIFOs have write-only memory ports; attach unused
        # read wires for this Migen simulator version, matching test_bankmachine.
        fragment = dut.get_fragment()
        for special in fragment.specials:
            if isinstance(special, Memory):
                for port in special.ports:
                    if port.dat_r is None:
                        port.dat_r = Signal(special.width)
        litex_run_simulation(fragment, bench())


if __name__ == "__main__":
    unittest.main()
