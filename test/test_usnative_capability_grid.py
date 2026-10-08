#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Source-envelope and DFI smoke grid; not native-PHY functional coverage.

The matrix checks both UltraScale families, DDR3/DDR4, x16/x32/x64, and 1:4
or 1:8 controller ratios. DDR3 cells are expected rejections. Accepted 1:8
cells exercise only generic DFI conversion in simulation; this does not model
SelectIO pins, memory commands, calibration, or electrical behavior.
"""

import unittest

from migen import Module
from migen.sim import run_simulation

from litedram.phy.dfi import DFIRateConverter, Interface
from litedram.phy.usnative.capabilities import validate_native_configuration


FAMILIES = ("ULTRASCALE", "ULTRASCALE_PLUS")
MEMTYPES = ("DDR3", "DDR4")
DQ_WIDTHS = (16, 32, 64)
RATIOS = ("1:4", "1:8")


class _RatioEightDUT(Module):
    def __init__(self, databits):
        # The native PHY runs at the fast 1:4 rate. The 1:8 controller uses
        # half-width phases and the converter serializes them onto that DFI.
        self.phy_dfi = Interface(addressbits=17, bankbits=3, nranks=1,
            databits=2*databits, nphases=4)
        self.submodules.converter = DFIRateConverter(self.phy_dfi,
            clkdiv="sys", clk="sys2x", ratio=2)
        self.controller_dfi = self.converter.dfi


class TestUSNativeCapabilityGrid(unittest.TestCase):
    def test_matrix_accepts_ddr4_and_expected_rejects_ddr3(self):
        accepted = rejected = 0
        for family in FAMILIES:
            for memtype in MEMTYPES:
                for width in DQ_WIDTHS:
                    for ratio in RATIOS:
                        with self.subTest(family=family, memtype=memtype,
                                          width=width, ratio=ratio):
                            if memtype == "DDR3":
                                with self.assertRaisesRegex(ValueError,
                                        "supports DDR4 only"):
                                    validate_native_configuration(family=family,
                                        memtype=memtype, databits=width,
                                        controller_ratio=ratio)
                                rejected += 1
                            else:
                                config = validate_native_configuration(
                                    family=family, memtype=memtype,
                                    databits=width, controller_ratio=ratio)
                                self.assertEqual(config.family, family)
                                self.assertEqual(config.memtype, "DDR4")
                                self.assertEqual(config.databits, width)
                                self.assertEqual(config.controller_ratio, ratio)
                                accepted += 1
        self.assertEqual((accepted, rejected), (12, 12))

    def test_validator_rejects_unknown_family_width_and_ratio(self):
        base = dict(family="ULTRASCALE", memtype="DDR4", databits=32,
                    controller_ratio="1:4")
        for field, value, message in (
                ("family", "VERSAL", "Unsupported native SelectIO family"),
                ("databits", 8, "Unsupported native DDR4 DQ width"),
                ("controller_ratio", "1:2", "Unsupported native controller ratio")):
            with self.subTest(field=field):
                config = dict(base)
                config[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    validate_native_configuration(**config)

    def test_supported_1to8_widths_simulate_generic_dfi_conversion(self):
        clocks = {"sys": (8, 3), "sys2x": (4, 1)}
        for family in FAMILIES:
            for width in DQ_WIDTHS:
                with self.subTest(family=family, width=width):
                    validate_native_configuration(family=family,
                        memtype="DDR4", databits=width,
                        controller_ratio="1:8")
                    dut = _RatioEightDUT(width)
                    controller, phy = dut.controller_dfi, dut.phy_dfi
                    self.assertEqual(len(controller.phases), 8)
                    self.assertEqual(len(controller.p0.wrdata), width)
                    self.assertEqual(len(phy.phases), 4)
                    self.assertEqual(len(phy.p0.wrdata), 2*width)
                    self.assertEqual(sum(len(p.wrdata) for p in controller.phases),
                                     8*width)

                    mask = (1 << width) - 1
                    byte_mask = (1 << (width//8)) - 1
                    data = [(0x1234 + phase*0x101) & mask
                            for phase in range(8)]
                    masks = [(phase + 1) & byte_mask for phase in range(8)]

                    def drive_controller():
                        for _ in range(24):
                            for phase, dfi_phase in enumerate(controller.phases):
                                yield dfi_phase.wrdata.eq(data[phase])
                                yield dfi_phase.wrdata_mask.eq(masks[phase])
                            yield

                    def check_phy():
                        for _ in range(8):
                            yield
                        for cycle in range(20):
                            active = ((cycle + 1) & 1) == 0
                            for phase, dfi_phase in enumerate(phy.phases):
                                expected = (data[2*phase] |
                                    (data[2*phase + 1] << width)) if active else 0
                                expected_mask = (masks[2*phase] |
                                    (masks[2*phase + 1] << (width//8))) if active else 0
                                self.assertEqual((yield dfi_phase.wrdata), expected)
                                self.assertEqual((yield dfi_phase.wrdata_mask), expected_mask)
                            yield

                    run_simulation(dut,
                        {"sys": [drive_controller()], "sys2x": [check_phy()]},
                        clocks=clocks)


if __name__ == "__main__":
    unittest.main()
