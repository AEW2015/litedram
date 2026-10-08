#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Reject DDR3 before the experimental DDR4 native PHY queries Vivado."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from migen import Signal

from litex.build.xilinx.vivado import XilinxVivadoToolchain
from litedram.phy.usnative.ddrphy import USNativeDDRPHY


class TestUSNativeDDR3Rejection(unittest.TestCase):
    def test_ddr3_request_stops_before_query_codegen_or_mapping(self):
        # This models the DDR3 command pins present on a normal DFI-facing
        # interface. DDR3 has no DDR4 BG/ACT_n fields or DM pin group.
        pads = SimpleNamespace(
            a=Signal(14), we_n=Signal(), cas_n=Signal(), ras_n=Signal(),
            ba=Signal(2), clk_p=Signal(), clk_n=Signal(), cs_n=Signal(),
            cke=Signal(), odt=Signal(), reset_n=Signal(), dq=Signal(16),
        )
        platform = SimpleNamespace(
            device="xcku040-fbva676-1-c",
            toolchain=XilinxVivadoToolchain(),
        )

        with patch("litedram.phy.usnative.ddrphy.extract_ddr_pins") as extract, \
             patch("litedram.phy.usnative.ddrphy.query_device") as query, \
             patch("litedram.phy.usnative.ddrphy.emit_core") as emit_core, \
             patch("litedram.phy.usnative.ddrphy.NativeMapping") as mapping:
            with self.assertRaisesRegex(ValueError, "single-rank DDR4"):
                USNativeDDRPHY(
                    pads, platform, Signal(), Signal(), Signal(),
                    sys_clk_freq=200e6, output_dir="unused", memtype="DDR3",
                )

        extract.assert_not_called()
        query.assert_not_called()
        emit_core.assert_not_called()
        mapping.assert_not_called()


if __name__ == "__main__":
    unittest.main()
