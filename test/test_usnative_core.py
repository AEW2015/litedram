#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Generated native core interfaces, complete wiring and invalid-layout rejection."""

import unittest

from dataclasses import replace
from migen import Instance, Record
from migen.sim import run_simulation

from litedram.phy.usnative.core import emit_core
from litedram.phy.usnative.control import control_profiles
from litedram.phy.usnative.sidebands import NativeSidebands, sideband_plan
from test.test_usnative_layout import layout_fixture


class TestUSNativeCore(unittest.TestCase):
    def test_width_bank_and_riu_coverage(self):
        for wide in (False, True):
            sites, auxiliary = layout_fixture(wide)
            core = emit_core('native_test', sites, auxiliary,
                             family='ULTRASCALE_PLUS', refclk_mhz=2400)
            self.assertEqual(core.ports['pll_clk'][1], 2 if wide else 1)
            self.assertEqual(core.ports['data_tristate'][1], 4 if wide else 2)
            self.assertEqual(core.verilog.count('RXTX_BITSLICE #('), len(core.layout.slices))
            self.assertEqual(core.verilog.count('BITSLICE_CONTROL #('), len(core.layout.controls))
            self.assertEqual(core.verilog.count('TX_BITSLICE_TRI #('), len(core.layout.controls))
            self.assertEqual(core.verilog.count('RIU_OR #('), len(core.layout.riu_bytes))
            for control in core.layout.controls:
                self.assertIn(f'LOC = "{control}"', core.verilog)
            self.assertNotIn('xem8320', core.verilog)

    def test_data_tbyte_is_opt_in_and_keeps_command_path(self):
        sites, auxiliary = layout_fixture()
        ordinary = emit_core('native_test', sites, auxiliary,
            family='ULTRASCALE_PLUS', refclk_mhz=2400)
        enabled = emit_core('native_test', sites, auxiliary,
            family='ULTRASCALE_PLUS', refclk_mhz=2400, data_tbyte=True)
        self.assertIn('.TBYTE_CTL("T")', ordinary.verilog)
        self.assertIn('.TBYTE_CTL("TBYTE_IN")', enabled.verilog)
        # The opt-in routes DQ/DM output enables through their native byte
        # control and leaves command TBYTE configuration unchanged. The core
        # only selects the physical path; it does not make the PHY's existing
        # write-leveling DQ policy equivalent to the direct per-lane T path.
        self.assertIn('.T(1\'b0)', enabled.verilog)
        self.assertRegex(enabled.verilog, r'\.TBYTE_IN\(tri_\d+\)')
        self.assertRegex(enabled.verilog, r'\.TBYTE_IN\(tbyte\[\d+:\d+\]\)')
        self.assertEqual(enabled.verilog.count('.TBYTE_CTL("TBYTE_IN")'),
                         ordinary.verilog.count('.TBYTE_CTL("TBYTE_IN")') +
                         sum(key[0] in ('dq', 'dm') for key in enabled.layout.slices))
        with self.assertRaises(ValueError):
            emit_core('native_test', sites, auxiliary,
                family='ULTRASCALE_PLUS', refclk_mhz=2400, data_tbyte=1)

    def test_electrical_options_are_explicit(self):
        sites, auxiliary = layout_fixture()
        ordinary = emit_core('native_test', sites, auxiliary,
                             family='ULTRASCALE_PLUS', refclk_mhz=2400)
        pre = emit_core('native_test', sites, auxiliary,
                        family='ULTRASCALE_PLUS', refclk_mhz=2400,
                        pre_emphasis=True)
        dynamic = emit_core('native_test', sites, auxiliary,
                            family='ULTRASCALE_PLUS', refclk_mhz=2400,
                            dynamic_odelay=True)
        self.assertIn('.ENABLE_PRE_EMPHASIS("FALSE")', ordinary.verilog)
        self.assertIn('.ENABLE_PRE_EMPHASIS("TRUE")', pre.verilog)
        self.assertIn('.EN_DYN_ODLY_MODE("FALSE")', ordinary.verilog)
        self.assertIn('.EN_DYN_ODLY_MODE("TRUE")', dynamic.verilog)
        self.assertEqual(dynamic.verilog.count('.EN_DYN_ODLY_MODE("TRUE")'),
                         sum(c.data for c in control_profiles(sites).values()))
        for option in ({'pre_emphasis': 1}, {'dynamic_odelay': 1}):
            with self.assertRaises(ValueError):
                emit_core('native_test', sites, auxiliary,
                          family='ULTRASCALE_PLUS', refclk_mhz=2400, **option)

    def test_dqs_fifo_write_clock_mapping_is_opt_in_and_byte_indexed(self):
        sites, auxiliary = layout_fixture()
        ordinary = emit_core('native_test', sites, auxiliary,
            family='ULTRASCALE_PLUS', refclk_mhz=2400)
        monitored = emit_core('native_test', sites, auxiliary,
            family='ULTRASCALE_PLUS', refclk_mhz=2400,
            dqs_wrclk_monitor=True)

        self.assertNotIn('dqs_wrclk', ordinary.ports)
        self.assertEqual(monitored.ports['dqs_wrclk'], ('output', len(monitored.layout.lanes)))
        for lane in monitored.layout.lanes:
            self.assertIn(
                f'.FIFO_WRCLK_OUT(dqs_wrclk[{lane.index}])',
                monitored.verilog)
        self.assertEqual(
            monitored.verilog.count('.FIFO_WRCLK_OUT(dqs_wrclk['),
            len(monitored.layout.lanes))
        with self.assertRaises(ValueError):
            emit_core('native_test', sites, auxiliary,
                family='ULTRASCALE_PLUS', refclk_mhz=2400,
                dqs_wrclk_monitor=1)

    def test_selected_dm_fifo_write_clocks_are_separate_from_dqs(self):
        sites, auxiliary = layout_fixture()
        # Put the two mask pins on nibble BITSLICE_0, as on AES-KU40.
        # The base synthetic fixture puts DQ0/DQ8 there instead.
        for lane in (0, 1):
            dq_key, dm_key = ('dq', 8*lane), ('dm', lane)
            sites[dq_key], sites[dm_key] = sites[dm_key], sites[dq_key]
        monitored = emit_core('native_test', sites, auxiliary,
            family='ULTRASCALE_PLUS', refclk_mhz=1600,
            dqs_wrclk_monitor=True, dm_wrclk_lanes=(0, 1))
        self.assertEqual(monitored.ports['dm_wrclk'], ('output', 2))
        for index in range(2):
            self.assertEqual(monitored.verilog.count(
                f'.FIFO_WRCLK_OUT(dm_wrclk[{index}])'), 1)
        self.assertEqual(monitored.verilog.count('.FIFO_WRCLK_OUT(dqs_wrclk['),
            len(monitored.layout.lanes))
        with self.assertRaisesRegex(ValueError, 'unique nonnegative'):
            emit_core('native_test', sites, auxiliary,
                family='ULTRASCALE_PLUS', refclk_mhz=1600,
                dm_wrclk_lanes=(0, 0))
        original, auxiliary = layout_fixture()
        with self.assertRaisesRegex(ValueError, 'BITSLICE_0'):
            emit_core('native_test', original, auxiliary,
                family='ULTRASCALE_PLUS', refclk_mhz=1600,
                dm_wrclk_lanes=(0,))

    def test_explicit_sideband_partition_and_mode_checks(self):
        sites, auxiliary = layout_fixture()
        sites.update({('par', 0): sites['dm', 0], ('alert_n', 0): sites['dm', 1]})
        plan = sideband_plan(sites, mr2=0x20, mr5=1 << 10)
        self.assertEqual(set(plan.native_sites) | set(plan.parity) | set(plan.alert), set(sites))
        self.assertEqual(plan.parity, (('par', 0),))
        self.assertEqual(plan.alert, (('alert_n', 0),))
        for mr2, mr5 in ((1 << 12, 0), (0, 1), (0, 7), (-1, 0), (0, True)):
            with self.assertRaises(ValueError):
                sideband_plan(sites, mr2=mr2, mr5=mr5)
        with self.assertRaises(ValueError):
            sideband_plan({**sites, ('parity', 0): sites['par', 0]}, mr2=0, mr5=0)

    def test_unknown_signal_cannot_disappear(self):
        sites, auxiliary = layout_fixture()
        sites['unknown', 0] = sites['dm', 0]
        with self.assertRaisesRegex(ValueError, 'Unsupported signal'):
            emit_core('native_test', sites, auxiliary, family='ULTRASCALE_PLUS', refclk_mhz=2400)

    def test_alert_is_masked_during_calibration(self):
        sites, _ = layout_fixture()
        sites['alert_n', 0] = replace(sites['dm', 0], position=6)
        plan = sideband_plan(sites, mr2=0, mr5=1024)
        self.assertTrue(plan.alert_unavailable_during_calibration)
        dut = NativeSidebands(Record([('alert_n', 1)]), plan)
        fragment = dut.get_fragment()
        fragment.specials = {s for s in fragment.specials if not isinstance(s, Instance)}
        def bench():
            # The physical pin is unavailable/low while BISC owns its site.
            yield dut._raw_alert_n.eq(0)
            for _ in range(5):
                yield
            self.assertEqual((yield dut.active), 0)
            self.assertEqual((yield dut.seen), 0)
            yield dut.enable.eq(1)
            for _ in range(3):
                yield
            self.assertEqual((yield dut.active), 1)
            self.assertEqual((yield dut.seen), 1)
            yield dut.clear.eq(1)
            for _ in range(2):
                yield
            self.assertEqual((yield dut.seen), 1)  # Active input wins over clear.
            yield dut.clear.eq(0)
            yield dut._raw_alert_n.eq(1)
            for _ in range(5):
                yield
            self.assertEqual((yield dut.active), 0)
            self.assertEqual((yield dut.seen), 1)
            yield dut.clear.eq(1)
            for _ in range(2):
                yield
            self.assertEqual((yield dut.seen), 0)
            yield dut.enable.eq(0)
            yield dut.clear.eq(0)
            yield dut._raw_alert_n.eq(0)
            for _ in range(5):
                yield
            self.assertEqual((yield dut.active), 0)
            self.assertEqual((yield dut.seen), 0)
        run_simulation(fragment, bench())
