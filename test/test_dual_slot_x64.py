#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

from types import SimpleNamespace
import unittest

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import LiteDRAMInterface
from litedram.core.crossbar import LiteDRAMCrossbar


class DualSlotX64AddressTest(unittest.TestCase):
    def test_four_group_mapping_routes_all_sixteen_banks_bijectively(self):
        settings = SimpleNamespace(
            phy=SimpleNamespace(nphases=8, read_latency=1, write_latency=1,
                                nranks=1, dfi_databits=128),
            geom=SimpleNamespace(rowbits=17, colbits=10, bankbits=4),
            with_dual_slot=True,
            with_bank_group_interleaving=True,
            address_mapping="ROW_BANK_COL",
            bank_byte_alignment=0,
            cmd_buffer_depth=8,
        )
        interface = LiteDRAMInterface(address_align=3, settings=settings)
        top = Module()
        crossbar = LiteDRAMCrossbar(interface)
        top.submodules.crossbar = crossbar
        port = crossbar.get_port(mode="both")

        def main():
            for bank in range(16):
                endpoint = getattr(interface, "bank" + str(bank))
                yield endpoint.ready.eq(1)
                yield endpoint.wdata_ready.eq(1)

            for bank in range(16):
                # In the native address, BG0 is bit 0, the two BA bits are
                # bits 8:10, and BG1 is bit 10. Keep row and column nonzero so
                # the row/column permutation is checked along with the bank.
                address = ((0x1234 << 11) | (0x35 << 1) |
                           ((bank & 0x3) << 8) |
                           (((bank >> 2) & 1) << 0) |
                           (((bank >> 3) & 1) << 10))
                yield port.cmd.addr.eq(address)
                yield port.cmd.we.eq(0)
                yield port.cmd.valid.eq(1)
                yield
                routed = []
                routed_address = None
                for candidate in range(16):
                    endpoint = getattr(interface, "bank" + str(candidate))
                    if (yield endpoint.valid):
                        routed.append(candidate)
                        routed_address = (yield endpoint.addr)
                self.assertEqual(routed, [bank])
                expected_rca = ((address >> 1) & 0x7f) | ((address >> 11) << 7)
                self.assertEqual(routed_address, expected_rca)
                self.assertEqual((yield port.cmd.ready), 1)
                yield port.cmd.valid.eq(0)
                yield

        fragment = top.get_fragment()
        # This project's pinned Migen simulator expects a read signal on
        # write-only FIFO memories created by the crossbar arbiters.
        for special in fragment.specials:
            if isinstance(special, Memory):
                for memory_port in special.ports:
                    if memory_port.dat_r is None:
                        memory_port.dat_r = Signal(special.width)
        run_simulation(fragment, main())


if __name__ == "__main__":
    unittest.main()
