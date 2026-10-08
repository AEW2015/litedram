#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Tests for the generic DDR3 DFI command/address mapping."""

import unittest

from migen import Module
from migen.sim import run_simulation

from litedram.phy.dfi import DDR3DFIMux, Interface


class _DDR3DFIMuxDUT(Module):
    def __init__(self, *, addressbits=14, bankbits=3, nranks=2, databits=32,
                 nphases=4):
        self.dfi_i = Interface(addressbits, bankbits, nranks, databits, nphases)
        self.dfi_o = Interface(addressbits, bankbits, nranks, databits, nphases)
        self.submodules.mapper = DDR3DFIMux(self.dfi_i, self.dfi_o)


class TestDDR3DFIMux(unittest.TestCase):
    def test_direct_command_address_mapping(self):
        dut = _DDR3DFIMuxDUT()
        commands = ((0, 1, 1), (1, 0, 1), (1, 0, 0), (0, 1, 0))

        def drive():
            for index, phase in enumerate(dut.dfi_i.phases):
                ras_n, cas_n, we_n = commands[index]
                yield phase.address.eq(0x123 + index)
                yield phase.bank.eq(index)
                yield phase.ras_n.eq(ras_n)
                yield phase.cas_n.eq(cas_n)
                yield phase.we_n.eq(we_n)
                yield phase.cs_n.eq(1 + (index & 1))
                yield phase.cke.eq(index & 1)
                yield phase.odt.eq((index + 1) & 1)
                yield phase.reset_n.eq(1)
                yield phase.act_n.eq(0)
                yield phase.wrdata.eq(0x10203040 + index)
                yield phase.wrdata_en.eq(index & 1)
                yield phase.wrdata_mask.eq(index & 0xf)
            yield

        def check():
            yield
            for index, phase in enumerate(dut.dfi_o.phases):
                ras_n, cas_n, we_n = commands[index]
                self.assertEqual((yield phase.address), 0x123 + index)
                self.assertEqual((yield phase.bank), index)
                self.assertEqual((yield phase.ras_n), ras_n)
                self.assertEqual((yield phase.cas_n), cas_n)
                self.assertEqual((yield phase.we_n), we_n)
                self.assertEqual((yield phase.cs_n), 1 + (index & 1))
                self.assertEqual((yield phase.cke), index & 1)
                self.assertEqual((yield phase.odt), (index + 1) & 1)
                self.assertEqual((yield phase.reset_n), 1)
                self.assertEqual((yield phase.act_n), 1)
                self.assertEqual((yield phase.wrdata), 0x10203040 + index)
                self.assertEqual((yield phase.wrdata_en), index & 1)
                self.assertEqual((yield phase.wrdata_mask), index & 0xf)
                yield

        run_simulation(dut, [drive(), check()])

    def test_rejects_mismatched_phase_count(self):
        dfi_i = Interface(14, 3, 1, 16, 4)
        dfi_o = Interface(14, 3, 1, 16, 2)
        with self.assertRaisesRegex(ValueError, "same phase count"):
            DDR3DFIMux(dfi_i, dfi_o)

    def test_rejects_mismatched_command_widths(self):
        dfi_i = Interface(14, 3, 1, 16, 1)
        dfi_o = Interface(15, 3, 1, 16, 1)
        with self.assertRaisesRegex(ValueError, "matching command widths"):
            DDR3DFIMux(dfi_i, dfi_o)

    def test_rejects_mismatched_data_widths(self):
        dfi_i = Interface(14, 3, 1, 16, 1)
        dfi_o = Interface(14, 3, 1, 32, 1)
        with self.assertRaisesRegex(ValueError, "matching data widths"):
            DDR3DFIMux(dfi_i, dfi_o)


if __name__ == "__main__":
    unittest.main()
