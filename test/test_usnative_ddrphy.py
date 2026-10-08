#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest
import ast
from pathlib import Path
from types import SimpleNamespace

from migen import If, Module, Signal
from migen.fhdl import verilog
from migen.sim import run_simulation

from litedram.common import TappedDelayLine
from litedram.phy.usnative.ddrphy import (
    NativeDebugTraceCapture, NativeRXBitslip, NativeWriteSnapshot, USNativeDDRPHY,
    _add_native_data_iobufs, _native_fabric_vref_lanes,
    _native_fabric_vref_profile,
    _native_data_phase, _native_effective_read_latency, _native_latency_profile,
    _native_gate_settings,
    _native_gate_active,
)
from litex.build.xilinx.vivado import XilinxVivadoToolchain


class NativeGateSettingsProbe(Module):
    def __init__(self, *, trained_gate_delays):
        self.software_control = Signal()
        self.selected = Signal()
        self.lane_delay = Signal(5)
        self.global_delay = Signal(5)
        self.lane_width = Signal(4)
        self.delay = Signal(5)
        self.width = Signal(4)
        self.gate = Signal()
        self.remaining = Signal(4)
        self.write_level = Signal()
        self.ready = Signal()
        self.phy_reset = Signal()
        self.active = Signal()
        lane_delay, lane_width = _native_gate_settings(
            self.software_control, trained_gate_delays, self.selected,
            self.lane_delay, self.global_delay, self.lane_width)
        self.comb += [
            self.delay.eq(lane_delay),
            self.width.eq(lane_width),
            self.active.eq(_native_gate_active(self.gate, self.remaining,
                self.write_level, self.ready, self.phy_reset)),
        ]


class TestNativeDataPhases(unittest.TestCase):
    @staticmethod
    def _vref_fixture(nbytes=2, *, split_dm=False):
        lanes = [SimpleNamespace(index=lane) for lane in range(nbytes)]
        physical = {}
        for lane in range(nbytes):
            base = lane * 13
            keys = ([('dq', bit) for bit in range(lane * 8, lane * 8 + 8)] +
                    [('dm', lane), ('dqs_p', lane), ('dqs_n', lane)])
            for offset, key in enumerate(keys):
                group = (0, lane + 1)
                if split_dm and key == ('dm', lane):
                    group = (0, lane + 101)
                physical[key] = SimpleNamespace(bank=group[0], byte=group[1],
                    position=base + offset if base + offset < 13 else offset)
        return SimpleNamespace(lanes=lanes), physical

    @staticmethod
    def _buffer_fixture(nbytes=2):
        databits = 8 * nbytes
        pads = SimpleNamespace(dq=Signal(databits), dm=Signal(nbytes))
        signals = {
            'o_dq_serial_out': Signal(databits),
            'o_dq_tristate': Signal(databits),
            'o_dm_n_serial_out': Signal(nbytes),
            'o_dm_n_tristate': Signal(nbytes),
            'o_dyn_dci': Signal(databits + nbytes),
            'i_dq_serial_in': Signal(databits),
            'i_dm_n_serial_in': Signal(nbytes),
        }
        return pads, signals, databits

    def test_fabric_receiver_vref_is_opt_in_and_groups_one_physical_x8_byte(self):
        self.assertEqual(_native_fabric_vref_profile(False), {})
        self.assertEqual(_native_fabric_vref_profile(True),
            {'fabric_receiver_vref': {'code': 29, 'mode': 'FABRIC_RANGE1'}})
        layout, physical = self._vref_fixture()
        self.assertEqual(_native_fabric_vref_lanes(layout, physical, 16),
            {0: (0, 1), 1: (0, 2)})

        default = Module()
        pads, signals, databits = self._buffer_fixture()
        _add_native_data_iobufs(default, pads, signals, layout, physical,
            databits=databits, dynamic_dci=True, fabric_receiver_vref=False)
        default_rtl = str(verilog.convert(default))
        self.assertEqual(default_rtl.count('IOBUF_DCIEN IOBUF_DCIEN'), 18)
        self.assertNotIn('HPIO_VREF', default_rtl)
        self.assertNotIn('IOBUFE3', default_rtl)

        enabled = Module()
        _add_native_data_iobufs(enabled, pads, signals, layout, physical,
            databits=databits, dynamic_dci=True, fabric_receiver_vref=True)
        enabled_rtl = str(verilog.convert(enabled))
        self.assertEqual(enabled_rtl.count('IOBUFE3 #('), 18)
        self.assertEqual(enabled_rtl.count('HPIO_VREF #('), 2)
        self.assertNotIn('IOBUF_DCIEN', enabled_rtl)
        self.assertEqual(enabled_rtl.count('FABRIC_RANGE1'), 2)
        self.assertEqual(enabled_rtl.count("7'd29"), 2)
        self.assertEqual(enabled_rtl.count('DONT_TOUCH = "TRUE"'), 2)
        lane0_vref = next(line.strip().split()[-1][:-1] for line in enabled_rtl.splitlines()
                          if line.strip().startswith('wire ')
                          and 'native_fabric_vref_lane0' in line)
        lane1_vref = next(line.strip().split()[-1][:-1] for line in enabled_rtl.splitlines()
                          if line.strip().startswith('wire ')
                          and 'native_fabric_vref_lane1' in line)
        self.assertEqual(enabled_rtl.count(f'.VREF({lane0_vref})'), 10)
        self.assertEqual(enabled_rtl.count(f'.VREF({lane1_vref})'), 10)
        for pin in ('.IBUFDISABLE(1\'d0)', '.DCITERMDISABLE(', '.T('):
            self.assertIn(pin, enabled_rtl)

    def test_fabric_receiver_vref_rejects_cross_group_or_missing_pad_maps(self):
        layout, physical = self._vref_fixture(split_dm=True)
        with self.assertRaisesRegex(ValueError, 'crosses physical 13-IO groups'):
            _native_fabric_vref_lanes(layout, physical, 16)
        layout, physical = self._vref_fixture()
        del physical[('dqs_n', 1)]
        with self.assertRaisesRegex(ValueError, 'missing physical pad'):
            _native_fabric_vref_lanes(layout, physical, 16)

    def test_fabric_receiver_vref_constructor_rejects_unsupported_target_early(self):
        for memtype, device, expected in (
                ('DDR3', 'xcvu3p-ffvc1517-2-e', 'only for native DDR4'),
                ('DDR4', 'xcvu3-ffvc1517-2-e', r'UltraScale\+ device')):
            with self.subTest(memtype=memtype, device=device):
                platform = SimpleNamespace(toolchain=XilinxVivadoToolchain(), device=device)
                with self.assertRaisesRegex(ValueError, expected):
                    USNativeDDRPHY(None, platform, None, None, None, sys_clk_freq=300e6,
                        output_dir='unused', fabric_receiver_vref=True, memtype=memtype)

    def test_2400_cl18_cwl16_profile_is_opt_in_and_exact(self):
        self.assertIsNone(_native_latency_profile(300e6, None))
        self.assertEqual(_native_latency_profile(300e6, '2400_cl18_cwl16'),
            (18, 16, 1, 3, 13, 4, 3))
        source = (Path(__file__).parents[1] / "litedram" / "phy" / "usnative" /
            "ddrphy.py").read_text(encoding="utf-8")
        self.assertIn("**({'latency_profile': latency_profile} if latency_profile is not None else {})",
            source)
        for frequency, profile in ((300e6, True), (300e6, 'unknown'),
                                   (333333333, '2400_cl18_cwl16')):
            with self.subTest(frequency=frequency, profile=profile):
                with self.assertRaisesRegex(ValueError, 'latency profile'):
                    _native_latency_profile(frequency, profile)

    def test_2400_cl18_cwl16_generates_matching_mode_registers(self):
        from types import SimpleNamespace
        from litedram.init import get_ddr4_phy_init_sequence

        phy = SimpleNamespace(memtype='DDR4', cl=18, cwl=16, nphases=4,
            tck=1/300e6, tccd=8, is_rdimm=False)
        timings = SimpleNamespace(tWR=5, fine_refresh_mode='1x')
        sequence, _ = get_ddr4_phy_init_sequence(phy, timings)
        mr0 = next(value for name, value, *_ in sequence if name.startswith('Load Mode Register 0'))
        mr2 = next(value for name, value, *_ in sequence if name.startswith('Load Mode Register 2'))
        mr0_cl_code = ((mr0 >> 2) & 1) | (((mr0 >> 4) & 7) << 1) | (((mr0 >> 12) & 1) << 4)
        self.assertEqual(mr0_cl_code, 0b01000)  # DDR4 CL18 encoding.
        self.assertEqual((mr2 >> 3) & 7, 0b101)  # DDR4 CWL16 encoding.

    def test_invalid_latency_profile_is_rejected_before_pad_or_topology_access(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        with self.assertRaisesRegex(ValueError, 'requires a 300 MHz PHY clock'):
            USNativeDDRPHY(None, platform, None, None, None,
                sys_clk_freq=333333333, output_dir='unused',
                latency_profile='2400_cl18_cwl16')
        with self.assertRaisesRegex(ValueError, 'Unsupported Native latency profile'):
            USNativeDDRPHY(None, platform, None, None, None,
                sys_clk_freq=300e6, output_dir='unused', latency_profile=True)

    def test_manual_and_controller_gate_selection_preserves_global_fallback_and_width_one(self):
        manual = NativeGateSettingsProbe(trained_gate_delays=False)
        controller = NativeGateSettingsProbe(trained_gate_delays=True)

        def check_manual():
            yield manual.lane_delay.eq(5)
            yield manual.global_delay.eq(3)
            yield manual.lane_width.eq(4)
            yield manual.software_control.eq(1)
            yield manual.selected.eq(1)
            yield
            self.assertEqual((yield manual.delay), 5)
            self.assertEqual((yield manual.width), 4)
            yield manual.selected.eq(0)
            yield
            self.assertEqual((yield manual.delay), 3)
            self.assertEqual((yield manual.width), 1)
            yield manual.selected.eq(1)
            yield manual.software_control.eq(0)
            yield
            self.assertEqual((yield manual.delay), 3)
            self.assertEqual((yield manual.width), 1)

        run_simulation(manual, check_manual())

        def check_controller():
            yield controller.lane_delay.eq(6)
            yield controller.global_delay.eq(3)
            yield controller.lane_width.eq(7)
            yield controller.software_control.eq(0)
            yield controller.selected.eq(1)
            yield
            self.assertEqual((yield controller.delay), 6)
            self.assertEqual((yield controller.width), 1)
            yield controller.selected.eq(0)
            yield
            self.assertEqual((yield controller.delay), 3)
            self.assertEqual((yield controller.width), 1)

        run_simulation(controller, check_controller())

    def test_phy_reset_immediately_clears_active_read_gate(self):
        dut = NativeGateSettingsProbe(trained_gate_delays=True)

        def tb():
            yield dut.gate.eq(1)
            yield dut.ready.eq(1)
            yield dut.phy_reset.eq(0)
            yield
            self.assertEqual((yield dut.active), 1)
            yield dut.phy_reset.eq(1)
            yield
            self.assertEqual((yield dut.active), 0)

        run_simulation(dut, tb())

    def test_trained_gate_delays_are_opt_in_abi_field(self):
        source = (Path(__file__).parents[1] / "litedram" / "phy" / "usnative" /
            "ddrphy.py").read_text(encoding="utf-8")
        self.assertIn("**({'trained_gate_delays': True} if trained_gate_delays else {})", source)
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        with self.assertRaisesRegex(ValueError, 'Trained gate-delay option must be boolean'):
            USNativeDDRPHY(None, platform, None, None, None,
                sys_clk_freq=333333333, output_dir='unused', trained_gate_delays=1)

    def test_rx_boundary_monitor_format_is_opt_in_abi_field(self):
        source = (Path(__file__).parents[1] / "litedram" / "phy" / "usnative" /
            "ddrphy.py").read_text(encoding="utf-8")
        self.assertIn("**({'rx_boundary_monitor_format': 2} if with_rx_boundary_monitor else {})",
            source)
        self.assertIn("format_version=2", source)
        self.assertIn("raw_dq_indexing='logical_dq_bits'", source)
        self.assertIn("raw_dq = logical_dq_words(signals['o_dq_rx_data'], selected_bits)",
            source)

    def test_fixed_fifo_pop_rejects_other_fifo_ownership_modes(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        for held_return in (None, 'following_edge'):
            with self.subTest(held_return=held_return):
                with self.assertRaisesRegex(ValueError, "requires held_rx_return='pop_edge'"):
                    USNativeDDRPHY(None, platform, None, None, None,
                        sys_clk_freq=333333333, output_dir='unused',
                        fixed_fifo_pop=True, held_rx_return=held_return)
        for mode in ('local_fifo_drain', 'registered_fifo_drain',
                     'registered_common_fifo_drain', 'read_token_fifo_drain',
                     'with_scheduled_fifo_pop', 'with_scheduled_fifo_return'):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
                    USNativeDDRPHY(None, platform, None, None, None,
                        sys_clk_freq=333333333, output_dir='unused',
                        fixed_fifo_pop=True, **{mode: True})

    def test_fixed_fifo_pop_monitor_pipeline_is_opt_in_and_requires_fixed_pop(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        with self.assertRaisesRegex(ValueError, 'requires fixed_fifo_pop'):
            USNativeDDRPHY(None, platform, None, None, None,
                sys_clk_freq=333333333, output_dir='unused',
                fixed_fifo_pop_monitor_pipeline=True)
        for value in (0, 1, 'true', None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'option must be boolean'):
                    USNativeDDRPHY(None, platform, None, None, None,
                        sys_clk_freq=333333333, output_dir='unused',
                        fixed_fifo_pop=True, held_rx_return='pop_edge',
                        fixed_fifo_pop_monitor_pipeline=value)

    def test_explicit_read_latency_is_a_final_effective_profile_override(self):
        self.assertEqual(_native_effective_read_latency(12, 'pop_edge'), 12)
        self.assertEqual(_native_effective_read_latency(12, 'pop_edge', 13), 13)
        self.assertEqual(_native_effective_read_latency(12, 'following_edge'), 13)
        self.assertEqual(_native_effective_read_latency(12, 'following_edge', 13), 13)
        self.assertEqual(_native_effective_read_latency(12, 'following_edge', 14), 14)
        self.assertEqual(_native_effective_read_latency(16, 'pop_edge', 17), 17)
        self.assertEqual(_native_effective_read_latency(16, 'pop_edge', 18), 18)
        for value in (True, False, 11, 19, 32, 13.0, '13'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'Read latency override'):
                    _native_effective_read_latency(12, 'pop_edge', value)
        with self.assertRaisesRegex(ValueError, 'Read latency override'):
            _native_effective_read_latency(12, 'following_edge', 12)

    def test_scheduled_native_return_requires_explicit_pop_experiment(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        with self.assertRaisesRegex(ValueError, "requires scheduled FIFO pop mode"):
            USNativeDDRPHY(None, platform, None, None, None,
                sys_clk_freq=200e6, output_dir="unused",
                with_scheduled_fifo_return=True)

    def test_rx_tx_refclk_attribute_override_requires_a_finite_positive_number(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        for value in (True, 0, -1, float("inf"), float("nan"), "300"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError,
                        "RXTX reference-frequency attribute must be finite and positive"):
                    USNativeDDRPHY(None, platform, None, None, None,
                        sys_clk_freq=200e6, output_dir="unused",
                        refclk_attribute_mhz=value)

    def test_refclk_attribute_override_only_changes_the_rxtx_generation_input(self):
        source = (Path(__file__).parents[1] / "litedram" / "phy" / "usnative" /
            "ddrphy.py").read_text(encoding="utf-8")
        self.assertIn("refclk_mhz = refclk_attribute_mhz", source)
        self.assertIn("refclk_mhz=refclk_mhz", source)
        self.assertIn("BITSLICE_CONTROL PLL_CLK remains the actual 1.6 GHz source", source)
        core = (Path(__file__).parents[1] / "litedram" / "phy" / "usnative" /
            "core.py").read_text(encoding="utf-8")
        self.assertIn("PLL_CLK=word('pll_clk'", core)

    def test_shared_tbyte_cannot_drive_dq_during_write_leveling(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        with self.assertRaisesRegex(ValueError, 'cannot release DQ while driving DQS'):
            USNativeDDRPHY(None, platform, None, None, None, sys_clk_freq=300e6,
                           output_dir='unused', data_tbyte=True)

    def test_registered_fifo_idle_cycle_cannot_sustain_reads(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        with self.assertRaisesRegex(ValueError, 'leaves DFI read-valid without a new FIFO word'):
            USNativeDDRPHY(None, platform, None, None, None, sys_clk_freq=300e6,
                           output_dir='unused', registered_fifo_drain=True)

    def test_dynamic_output_delay_requires_validated_tap_restoration(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        with self.assertRaisesRegex(ValueError, 'per-DQ scan restoration'):
            USNativeDDRPHY(None, platform, None, None, None, sys_clk_freq=300e6,
                           output_dir='unused', dynamic_odelay=True)

    def test_wide_rx_trace_can_be_disabled_without_losing_gate_and_pop_monitor_csrs(self):
        source_path = (Path(__file__).parents[1] / "litedram" / "phy" /
                       "usnative" / "ddrphy.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        phy_class = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "USNativeDDRPHY")
        constructor = next(node for node in phy_class.body
                           if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        trace_arg = next(arg for arg in constructor.args.kwonlyargs
                         if arg.arg == "with_rx_trace")
        trace_arg_index = constructor.args.kwonlyargs.index(trace_arg)
        self.assertIsInstance(constructor.args.kw_defaults[trace_arg_index], ast.Constant)
        self.assertIsNone(constructor.args.kw_defaults[trace_arg_index].value)

        # Gate phase controls and the compact accepted-pop scoreboard remain
        # tied to with_read_monitor. Only wide-ring layout allocation keys off
        # the independent optional with_rx_trace parameter.
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("if with_read_monitor:\n            # Debug-only sub-cycle gate phase", source)
        self.assertIn("if with_rx_trace:\n            # Capture raw Q, DFI data", source)
        self.assertIn("if with_read_monitor:\n            from .rx_trace import NativeRXFIFOStatusTrace, NativeRXLanePopScoreboard", source)
        self.assertIn("if with_mrs_command_trace and not with_rx_trace:", source)
        self.assertIn("if with_rx_trace is None:\n            with_rx_trace = with_read_monitor", source)

        # Evaluate the constructor's actual optional profile-field condition:
        # legacy read-monitor-off default omits it; lean mode records false.
        mapping_condition = next(node.test for node in ast.walk(constructor)
            if isinstance(node, ast.IfExp) and isinstance(node.body, ast.Dict)
            and any(isinstance(key, ast.Constant) and key.value == "with_rx_trace"
                    for key in node.body.keys))
        condition = compile(ast.Expression(mapping_condition), str(source_path), "eval")
        self.assertFalse(eval(condition, {}, {"with_read_monitor": False, "with_rx_trace": False}))
        self.assertTrue(eval(condition, {}, {"with_read_monitor": True, "with_rx_trace": False}))

    def test_static_dci_is_opt_in_and_drives_iobuf_term_disable_low(self):
        platform = SimpleNamespace(toolchain=XilinxVivadoToolchain())
        with self.assertRaisesRegex(ValueError, 'Dynamic DCI option must be boolean'):
            USNativeDDRPHY(None, platform, None, None, None, sys_clk_freq=300e6,
                           output_dir='unused', dynamic_dci=1)

        source_path = (Path(__file__).parents[1] / "litedram" / "phy" /
                       "usnative" / "ddrphy.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        phy_class = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "USNativeDDRPHY")
        constructor = next(node for node in phy_class.body
                           if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        dynamic_default = next(arg for arg in constructor.args.kwonlyargs
                               if arg.arg == "dynamic_dci")
        default_index = constructor.args.kwonlyargs.index(dynamic_default)
        self.assertIsInstance(constructor.args.kw_defaults[default_index], ast.Constant)
        self.assertIs(constructor.args.kw_defaults[default_index].value, True)

        helper = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "_add_native_data_iobufs")
        iobuf = next(node for node in ast.walk(helper)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "Instance" and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "IOBUF_DCIEN")
        ports_assignment = next(node for node in ast.walk(helper)
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "ports"
                for target in node.targets))
        dci_input = next(keyword.value for keyword in ports_assignment.value.keywords
                         if keyword.arg == "i_DCITERMDISABLE")
        self.assertIsInstance(dci_input, ast.IfExp)
        self.assertIsInstance(dci_input.test, ast.Name)
        self.assertEqual(dci_input.test.id, "dynamic_dci")
        self.assertIsInstance(dci_input.body, ast.Subscript)
        self.assertIsInstance(dci_input.body.value, ast.Subscript)
        self.assertEqual(ast.literal_eval(dci_input.body.value.slice), "o_dyn_dci")
        self.assertIsInstance(dci_input.orelse, ast.Call)
        self.assertIsInstance(dci_input.orelse.func, ast.Name)
        self.assertEqual(dci_input.orelse.func.id, "Constant")
        self.assertEqual(ast.literal_eval(dci_input.orelse.args[0]), 0)

        # The default must remain ABI-stable; only static mode adds a profile
        # field so older/default mapping descriptors do not change.
        self.assertIn("**({'dynamic_dci': False} if not dynamic_dci else {})",
                      source_path.read_text(encoding="utf-8"))

    def test_profile_phases_include_native_command_cycle(self):
        profiles = (
            (11, 9, 0, 2),
            (13, 10, 2, 1),
            (15, 11, 0, 0),
            (17, 12, 2, 3),
            (19, 14, 0, 1),
            (21, 16, 2, 3),
            (24, 16, 3, 3),
        )
        for cl, cwl, rdphase, wrphase in profiles:
            with self.subTest(cl=cl, cwl=cwl):
                self.assertEqual(_native_data_phase(cl), rdphase)
                self.assertEqual(_native_data_phase(cwl), wrphase)

    def test_profile_write_latency_matches_registered_ca_and_tx_launch_pipeline(self):
        # Read the constructor's actual profile table so this regression cannot
        # pass by checking a second, stale copy of the supported timings.
        source_path = (Path(__file__).parents[1] / "litedram" / "phy" /
                       "usnative" / "ddrphy.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        phy_class = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "USNativeDDRPHY")
        constructor = next(node for node in phy_class.body
                           if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        profile_assignments = [node for node in ast.walk(constructor)
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "profiles"
                for target in node.targets)]
        self.assertEqual(len(profile_assignments), 1)
        profile_table = {}
        for key, value in zip(profile_assignments[0].value.keys,
                              profile_assignments[0].value.values):
            self.assertIsInstance(value, ast.Tuple)
            frequency = ast.literal_eval(key)
            cwl = ast.literal_eval(value.elts[1])
            wrphase_expression = value.elts[3]
            write_latency = ast.literal_eval(value.elts[5])
            profile_table[frequency] = (cwl, wrphase_expression, write_latency)

        # The CA slot register adds four CKs; the existing TX bitslip plus
        # final registered launch accounts for the remaining two fabric
        # cycles. With a four-phase x64 interface, the programmed PHY latency
        # therefore satisfies (4 + wrphase + CWL + 1) / 4 = write_latency + 2.
        registered_ca_ck = 4
        tx_pipeline_fabric_cycles = 2
        extra_write_ck = 1
        for frequency, (cwl, wrphase_expression, write_latency) in profile_table.items():
            self.assertIsInstance(wrphase_expression, ast.Call)
            self.assertIsInstance(wrphase_expression.func, ast.Name)
            self.assertEqual(wrphase_expression.func.id, "_native_data_phase")
            self.assertEqual(ast.literal_eval(wrphase_expression.args[0]), cwl)
            wrphase = _native_data_phase(cwl)
            with self.subTest(frequency=frequency, cwl=cwl, wrphase=wrphase):
                total_ck = registered_ca_ck + wrphase + cwl + extra_write_ck
                self.assertEqual(total_ck % 4, 0)
                self.assertEqual(total_ck // 4, write_latency + tx_pipeline_fabric_cycles)


class TestNativeRXBitslip(unittest.TestCase):
    def test_profile_read_latency_samples_two_back_to_back_skewed_lane_returns(self):
        # Derive the supported PHY read latencies from the constructor's real
        # profile table. Following-edge capture has one additional sys cycle.
        source_path = (Path(__file__).parents[1] / "litedram" / "phy" /
                       "usnative" / "ddrphy.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        phy_class = next(node for node in tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "USNativeDDRPHY")
        constructor = next(node for node in phy_class.body
                           if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        profile_assignment = next(node for node in ast.walk(constructor)
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "profiles"
                for target in node.targets))
        profile_latencies = {}
        for key, profile in zip(profile_assignment.value.keys,
                                profile_assignment.value.values):
            frequency = ast.literal_eval(key)
            if frequency in (200000000, 300000000, 333333333):
                profile_latencies[frequency] = ast.literal_eval(profile.elts[4])
        self.assertEqual(profile_latencies, {
            200000000: 11, 300000000: 12, 333333333: 12,
        })
        # The actual constructor resolves the fixed profile, held-return mode,
        # and optional final override before mapping/settings are constructed.
        effective_latency = next(node for node in ast.walk(constructor)
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "read_latency"
                for target in node.targets) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_native_effective_read_latency")
        mapping_call = next(node for node in ast.walk(constructor)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "NativeMapping")
        settings_call = next(node for node in ast.walk(constructor)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "PhySettings")
        self.assertLess(effective_latency.lineno, mapping_call.lineno)
        self.assertLess(effective_latency.lineno, settings_call.lineno)
        override_descriptor = next(node for node in ast.walk(mapping_call)
            if isinstance(node, ast.Dict) and any(isinstance(key, ast.Constant)
                and key.value == "read_latency_override" for key in node.keys))
        self.assertLess(effective_latency.lineno, override_descriptor.lineno)

        def returned_words(read_latency, held_return):
            read_request = Signal()
            pops = [Signal(name="pop_lane{}".format(lane)) for lane in range(2)]
            q = [Signal(8, name="q_lane{}".format(lane)) for lane in range(2)]
            reset = [Signal() for _ in range(2)]
            slip = [Signal() for _ in range(2)]
            rx = [NativeRXBitslip(q[lane], reset[lane], slip[lane],
                held_return=held_return, accepted=pops[lane]) for lane in range(2)]
            fixed_valid = TappedDelayLine(read_request, ntaps=read_latency)
            dut = Module()
            dut.submodules.fixed_valid = fixed_valid
            for lane in range(2):
                setattr(dut.submodules, "rx{}".format(lane), rx[lane])
            samples = []

            def stimulus():
                # Synthetic stable-Q timing model: the selected popped word
                # remains on Q through a following-edge capture. This checks
                # latency alignment under that explicit assumption only; it
                # does not model FIFO-head advancement or primitive behavior.
                values = ((0x11, 0x22), (0xa1, 0xb2))
                previous_pop_value = [0, 0]
                for cycle in range(20):
                    yield read_request.eq(cycle in (0, 2))
                    for lane in range(2):
                        pop_cycles = (9 + lane, 11 + lane)
                        pop_index = pop_cycles.index(cycle) if cycle in pop_cycles else None
                        if pop_index is not None:
                            previous_pop_value[lane] = values[pop_index][lane]
                        yield pops[lane].eq(pop_index is not None)
                        q_value = previous_pop_value[lane]
                        yield q[lane].eq(q_value)
                        if pop_index is None:
                            previous_pop_value[lane] = 0
                    yield
                    if (yield fixed_valid.taps[read_latency - 1]):
                        samples.append(((yield rx[0].o), (yield rx[1].o)))

            run_simulation(dut, stimulus())
            return samples

        base_latency = profile_latencies[200000000]
        for mode, latency in (("pop_edge", base_latency),
                              ("following_edge", base_latency + 1)):
            with self.subTest(mode=mode, latency=latency):
                self.assertEqual(returned_words(latency, mode),
                    [(0x11, 0x22), (0xa1, 0xb2)])
        # Without the following-edge cycle, the last lane is not yet captured
        # at the first fixed-valid sample.
        self.assertNotEqual(returned_words(base_latency, "following_edge")[0],
                            (0x11, 0x22))

    def test_following_edge_can_capture_next_prefetched_fifo_head(self):
        # Hardware traces show Q/head advancing one cycle after an accepted
        # pop (old head plus pop, then next head). Model that explicit stream:
        # pop_edge captures the word present on the accepted edge, whereas
        # following_edge captures the next FIFO head. This is a timing-model
        # demonstration, not a claim about every primitive/FIFO contract.
        modes = ("pop_edge", "following_edge")
        outputs = {}
        for mode in modes:
            pop = [Signal(name="lane{}_pop".format(lane)) for lane in range(2)]
            q = [Signal(8, name="lane{}_q".format(lane)) for lane in range(2)]
            reset = [Signal() for _ in range(2)]
            slip = [Signal() for _ in range(2)]
            rx = [NativeRXBitslip(q[lane], reset[lane], slip[lane],
                held_return=mode, accepted=pop[lane]) for lane in range(2)]
            dut = Module()
            for lane in range(2):
                setattr(dut.submodules, "rx{}".format(lane), rx[lane])
            samples = {}
            lane_words = ((0x11, 0xa1, 0xe1), (0x22, 0xb2, 0xe2))
            head_index = [0, 0]
            pop_cycles = ({3, 8}, {4, 9})

            def stimulus():
                for cycle in range(13):
                    for lane in range(2):
                        accepted = cycle in pop_cycles[lane]
                        yield pop[lane].eq(accepted)
                        yield q[lane].eq(lane_words[lane][head_index[lane]])
                    yield
                    for lane in range(2):
                        if cycle in pop_cycles[lane]:
                            head_index[lane] += 1
                    if cycle in (7, 11):
                        samples[cycle] = ((yield rx[0].o), (yield rx[1].o))

            run_simulation(dut, stimulus())
            outputs[mode] = samples

        self.assertEqual(outputs["pop_edge"], {
            7: (0x11, 0x22),
            11: (0xa1, 0xb2),
        })
        self.assertEqual(outputs["following_edge"], {
            7: (0xa1, 0xb2),
            11: (0xe1, 0xe2),
        })

    def test_rotates_each_captured_burst_without_joining_words(self):
        data, reset, slip = Signal(8), Signal(), Signal()
        dut = NativeRXBitslip(data, reset, slip)

        def stimulus():
            yield reset.eq(1)
            yield
            yield
            yield reset.eq(0)
            for rotation in range(8):
                # Alternating bursts expose any accidental use of the preceding
                # FIFO word, including at the wraparound boundary.
                for value in (0x96, 0x31, 0xE8):
                    yield data.eq(value)
                    yield
                    yield
                    expected = ((value >> rotation) |
                                (value << (8 - rotation))) & 0xff
                    self.assertEqual((yield dut.o), expected)
                yield slip.eq(1)
                yield
                yield slip.eq(0)
                yield
            yield data.eq(0x53)
            yield
            yield
            self.assertEqual((yield dut.o), 0x53)
            yield slip.eq(1)
            yield reset.eq(1)  # Reset takes priority over a simultaneous slip.
            yield
            yield
            self.assertEqual((yield dut.o), 0x53)

        run_simulation(dut, stimulus())

    def test_held_return_preserves_skewed_lane_words_until_fixed_valid(self):
        class FIFOQ(Module):
            def __init__(self, pop, tag):
                self.q = Signal(8)
                self.sync += If(pop,
                    self.q.eq(tag)
                ).Else(
                    self.q.eq(0)
                )

        reset, slip = Signal(), Signal()
        pop = [Signal(name="lane{}_accepted_pop".format(lane)) for lane in range(2)]
        q = [FIFOQ(pop[lane], tag) for lane, tag in enumerate((0xa5, 0x3c))]
        rx = [NativeRXBitslip(q[lane].q, reset, slip,
            held_return="following_edge", accepted=pop[lane]) for lane in range(2)]
        dut = Module()
        for lane in range(2):
            setattr(dut.submodules, "fifo_q{}".format(lane), q[lane])
            setattr(dut.submodules, "rx{}".format(lane), rx[lane])

        def stimulus():
            yield reset.eq(1)
            yield
            yield reset.eq(0)
            # Lane 0 pops one native clock before lane 1. The modeled FIFO
            # updates Q after its accepted edge and clears Q on the next edge.
            yield pop[0].eq(1)
            yield
            yield pop[0].eq(0)
            yield pop[1].eq(1)
            yield
            # At this edge lane 0's held word captures its pre-edge Q value.
            yield pop[1].eq(0)
            yield
            # Both words must survive the Q buses returning to zero while the
            # fixed-latency DFI read-valid arrives several clocks later.
            yield
            yield
            yield
            self.assertEqual((yield rx[0].o), 0xa5)
            self.assertEqual((yield rx[1].o), 0x3c)

        run_simulation(dut, stimulus())

    def test_held_return_pop_edge_candidate_captures_same_edge_q(self):
        data, accepted, reset, slip = Signal(8), Signal(), Signal(), Signal()
        dut = NativeRXBitslip(data, reset, slip,
            held_return="pop_edge", accepted=accepted)

        def stimulus():
            yield reset.eq(1)
            yield
            yield reset.eq(0)
            # This model makes Q valid before the edge that accepts the pop.
            yield data.eq(0x69)
            yield accepted.eq(1)
            yield
            yield data.eq(0)
            yield accepted.eq(0)
            yield
            yield
            self.assertEqual((yield dut.o), 0x69)

        run_simulation(dut, stimulus())

    def test_held_return_reset_clears_word_and_cancels_pending_capture(self):
        for held_return in ("pop_edge", "following_edge"):
            with self.subTest(held_return=held_return):
                data, accepted, reset, slip = Signal(8), Signal(), Signal(), Signal()
                dut = NativeRXBitslip(data, reset, slip,
                    held_return=held_return, accepted=accepted)

                def stimulus():
                    yield reset.eq(1)
                    yield
                    self.assertEqual((yield dut.o), 0)
                    yield reset.eq(0)
                    yield data.eq(0xa5)
                    yield accepted.eq(1)
                    yield
                    if held_return == "following_edge":
                        # Pending marks the accepted edge; capture Q at the
                        # following edge, then let the FIFO Q bus clear.
                        yield accepted.eq(0)
                        yield data.eq(0xa5)
                        yield
                    yield accepted.eq(0)
                    yield data.eq(0)
                    yield
                    self.assertEqual((yield dut.o), 0xa5)

                    # Reset must flush a previously captured held word.
                    yield reset.eq(1)
                    yield
                    yield
                    self.assertEqual((yield dut.o), 0)
                    yield reset.eq(0)
                    yield data.eq(0x5a)
                    yield accepted.eq(0)
                    yield
                    yield
                    self.assertEqual((yield dut.o), 0)

                    if held_return == "following_edge":
                        # An accepted edge coincident with reset must not
                        # survive in pending and capture after reset release.
                        yield accepted.eq(1)
                        yield data.eq(0x3c)
                        yield reset.eq(1)
                        yield
                        yield
                        self.assertEqual((yield dut.o), 0)
                        yield accepted.eq(0)
                        yield reset.eq(0)
                        yield data.eq(0x69)
                        yield
                        yield
                        self.assertEqual((yield dut.o), 0)

                run_simulation(dut, stimulus())


class TestNativeWriteSnapshot(unittest.TestCase):
    def test_captures_active_command_phase_history_and_all_write_phases(self):
        phases = []
        for index in range(4):
            phases.append(SimpleNamespace(
                address=Signal(17), bank=Signal(3), act_n=Signal(reset=1),
                ras_n=Signal(reset=1), cas_n=Signal(reset=1), we_n=Signal(reset=1),
                cs_n=Signal(reset=1), cke=Signal(reset=1), odt=Signal(),
                wrdata=Signal(64), wrdata_mask=Signal(8), wrdata_en=Signal()))
        dut = NativeWriteSnapshot(phases)

        def stimulus():
            yield dut.clear.eq(1)
            yield
            yield dut.clear.eq(0)
            # A command in phase 1 precedes the write-data enable by one cycle.
            yield phases[1].cs_n.eq(0)
            yield phases[1].address.eq(0x12345)
            yield phases[1].bank.eq(5)
            yield phases[1].cas_n.eq(0)
            yield phases[1].we_n.eq(0)
            yield
            yield phases[1].cs_n.eq(1)
            for index, phase in enumerate(phases):
                yield phase.address.eq(0x100 + index)
                yield phase.bank.eq(index)
                yield phase.wrdata.eq(0x1122334455667788 + index)
                yield phase.wrdata_mask.eq(0x80 | index)
                yield phase.wrdata_en.eq(index == 2)
            yield phases[2].cs_n.eq(0)
            yield phases[2].address.eq(0xabc)
            yield dut.trigger.eq(1)
            yield
            yield
            # Trigger is sampled with the raw frame, then snapshot appears
            # one active edge later; deassert only after the sample edge.
            yield dut.trigger.eq(0)
            yield
            self.assertEqual((yield dut.valid), 1)
            self.assertEqual((yield dut.count), 1)
            self.assertEqual((yield dut.command), 0xde)  # phase 2, valid
            self.assertEqual((yield dut.address), 0xabc)
            command = (yield dut.words[0])
            self.assertEqual(command & 0xfffffff,
                1 | (1 << 1) | (0x12345 << 3) | (5 << 20) |
                (1 << 25) | (1 << 27))
            self.assertEqual(len(dut.words), 18)
            # Current phase CA vector begins after four history words.
            ca_words = []
            for word in dut.words[4:8]:
                ca_words.append((yield word))
            ca = sum(value << (32 * index) for index, value in enumerate(ca_words))
            self.assertEqual(ca & ((1 << 27) - 1),
                0x100 | (0 << 17) | (1 << 20) | (1 << 21) | (1 << 22) |
                (1 << 23) | (1 << 24) | (1 << 25))
            data_words = []
            for word in dut.words[dut.data_word_offset:]:
                data_words.append((yield word))
            data = sum(value << (32 * index) for index, value in enumerate(data_words))
            phase0_data = data & ((1 << 73) - 1)
            self.assertEqual(phase0_data & ((1 << 64) - 1), 0x1122334455667788)
            self.assertEqual((phase0_data >> 64) & 0xff, 0x80)
            phase2_data = (data >> (2 * 73)) & ((1 << 73) - 1)
            self.assertEqual(phase2_data & ((1 << 64) - 1), 0x112233445566778a)
            self.assertEqual((phase2_data >> 64) & 0xff, 0x82)
            self.assertEqual((phase2_data >> 72) & 1, 1)
            phase3_data = (data >> (3 * 73)) & ((1 << 73) - 1)
            self.assertEqual(phase3_data & ((1 << 64) - 1), 0x112233445566778b)
            self.assertEqual((phase3_data >> 64) & 0xff, 0x83)
            yield dut.clear.eq(1)
            yield
            yield
            self.assertEqual((yield dut.valid), 0)
            self.assertEqual((yield dut.count), 0)
            self.assertEqual((yield dut.words[0]), 0)

        run_simulation(dut, stimulus())

    def test_trigger_stage_keeps_back_to_back_frames_atomic_and_clear_cancels_pending(self):
        phases = []
        for _ in range(4):
            phases.append(SimpleNamespace(
                address=Signal(17), bank=Signal(3), act_n=Signal(reset=1),
                ras_n=Signal(reset=1), cas_n=Signal(reset=1), we_n=Signal(reset=1),
                cs_n=Signal(reset=1), cke=Signal(reset=1), odt=Signal(),
                wrdata=Signal(64), wrdata_mask=Signal(8), wrdata_en=Signal()))
        dut = NativeWriteSnapshot(phases)

        def stimulus():
            yield dut.clear.eq(1)
            yield
            yield dut.clear.eq(0)
            # Keep the trigger asserted across adjacent input frames. Every
            # emitted snapshot must pair address and data from one frame.
            yield phases[0].cs_n.eq(0)
            yield phases[0].cas_n.eq(0)
            yield phases[0].address.eq(0x111)
            yield phases[0].wrdata.eq(0x1122334455667788)
            yield phases[0].wrdata_en.eq(1)
            yield dut.trigger.eq(1)
            for _ in range(5):
                yield
                if (yield dut.valid):
                    self.assertEqual((yield dut.address), 0x111)
                    data = 0
                    for index, word in enumerate(dut.words[dut.data_word_offset:]):
                        data |= (yield word) << (32*index)
                    self.assertEqual(data & ((1 << 64)-1), 0x1122334455667788)

            yield phases[0].address.eq(0x222)
            yield phases[0].wrdata.eq(0x8877665544332211)
            for _ in range(5):
                yield
                if (yield dut.valid):
                    address = (yield dut.address)
                    data = 0
                    for index, word in enumerate(dut.words[dut.data_word_offset:]):
                        data |= (yield word) << (32*index)
                    self.assertIn(address, (0x111, 0x222))
                    expected = (0x1122334455667788 if address == 0x111
                        else 0x8877665544332211)
                    self.assertEqual(data & ((1 << 64)-1), expected)
            self.assertEqual((yield dut.address), 0x222)
            self.assertGreaterEqual((yield dut.count), 2)

            # Stage another request and clear before allowing its output to
            # remain visible. Clear dominates the buffered trigger.
            yield phases[0].address.eq(0x333)
            yield phases[0].wrdata.eq(0x3333333333333333)
            yield dut.trigger.eq(1)
            for _ in range(3):
                yield
            self.assertEqual((yield dut._staged_trigger), 1)
            self.assertEqual((yield dut.address), 0x333)
            yield dut.clear.eq(1)
            yield
            yield
            self.assertEqual((yield dut.valid), 0)
            self.assertEqual((yield dut.count), 0)
            self.assertEqual((yield dut.address), 0)
            self.assertEqual((yield dut.words[0]), 0)
            yield dut.trigger.eq(0)
            yield
            yield dut.clear.eq(0)
            yield
            yield
            self.assertEqual((yield dut.valid), 0)
            self.assertEqual((yield dut.count), 0)

        run_simulation(dut, stimulus())

    def test_input_boundary_attributes_survive_verilog_conversion(self):
        phases = [SimpleNamespace(
            address=Signal(17), bank=Signal(3), act_n=Signal(reset=1),
            ras_n=Signal(reset=1), cas_n=Signal(reset=1), we_n=Signal(reset=1),
            cs_n=Signal(reset=1), cke=Signal(reset=1), odt=Signal(),
            wrdata=Signal(64), wrdata_mask=Signal(8), wrdata_en=Signal())
            for _ in range(4)]
        dut = NativeWriteSnapshot(phases)
        source = verilog.convert(dut, ios={dut.trigger, dut.clear}).main_source
        self.assertIn('keep = "true"', source)
        self.assertIn('dont_touch = "true"', source)
        self.assertIn('write_snapshot_stage_trigger', source)
        self.assertIn('write_snapshot_stage_p0_address', source)


class TestNativeDebugTraceCapture(unittest.TestCase):
    def test_trigger_before_arm_is_discarded(self):
        dut = NativeDebugTraceCapture(sample_width=16)

        def stimulus():
            yield dut.trigger.eq(1)
            yield dut.sample.eq(0x1111)
            yield
            yield
            self.assertEqual((yield dut._staged_trigger), 1)
            # Arming clears any pre-existing staged trigger. Once the source
            # trigger is low, the newly armed ring remains pending, not running.
            yield dut.arm.eq(1)
            yield dut.trigger.eq(0)
            yield
            yield dut.arm.eq(0)
            yield
            yield
            self.assertEqual((yield dut.pending), 1)
            self.assertEqual((yield dut.running), 0)
            self.assertEqual((yield dut.write_enable), 0)
            self.assertEqual((yield dut.done), 0)

        run_simulation(dut, stimulus())

    def test_staged_trigger_keeps_first_post_trigger_ring_sample(self):
        dut = NativeDebugTraceCapture(sample_width=16)
        captured = []

        def stimulus():
            yield dut.arm.eq(1)
            yield dut.sample.eq(0xDEAD)
            yield
            yield dut.arm.eq(0)
            yield dut.sample.eq(0xBEEF)
            yield
            # Legacy semantics start at the source cycle after the trigger.
            yield dut.sample.eq(0xAAAA)
            yield dut.trigger.eq(1)
            yield
            yield dut.trigger.eq(0)
            yield dut.sample.eq(0x100)
            yield
            # The first RAM write is enabled now. Capture its actual address
            # and data inputs at every write edge while changing the source.
            for _ in range(70):
                if (yield dut.done):
                    break
                if (yield dut.write_enable):
                    captured.append(((yield dut.write_address), (yield dut.write_data)))
                # The Migen driver update itself is applied at the cycle
                # boundary, so drive one source word ahead of the next staged
                # RAM edge.
                yield dut.sample.eq(0x101 + len(captured))
                yield
            self.assertEqual((yield dut.done), 1)
            self.assertEqual(captured, [(index, 0x100 + index) for index in range(64)])
        run_simulation(dut, stimulus())

    def test_input_stage_registers_and_trigger_boundary_survive_verilog(self):
        dut = NativeDebugTraceCapture(sample_width=64)
        source = verilog.convert(dut, ios={dut.arm, dut.trigger, dut.sample}).main_source
        self.assertIn('keep = "true"', source)
        self.assertIn('dont_touch = "true"', source)
        self.assertIn('debug_trace_stage_sample', source)
        self.assertIn('debug_trace_stage_trigger', source)
