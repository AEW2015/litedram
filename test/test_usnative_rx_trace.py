#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest
from types import SimpleNamespace

from migen.sim import run_simulation
from migen.fhdl import verilog
from migen import Module, Signal

from litedram.phy.usnative.rx_trace import (
    NativeReadValidSnapshot, NativeRXFIFOStatusTrace,
    NativeRXLanePopScoreboard, NativeRXTrace, NativeRXTraceLayout,
    NativeRXBoundaryTrace, NativeRXBoundaryTraceLayout, logical_dq_words,
    logical_dfi_lane_words,
)


class NativeRXTraceTest(unittest.TestCase):
    def test_selected_lane_trace_keeps_full_context_and_compacts_data_fields(self):
        full = NativeRXTraceLayout(databits=64, ntaps=106,
            dfi_data_width=512, ncontrols=22, ca_width=40)
        selected = NativeRXTraceLayout(databits=64, ntaps=106,
            dfi_data_width=512, ncontrols=22, ca_width=40, selected_lane=1)
        self.assertEqual(full.width, 2292)
        self.assertEqual(selected.width, 780)
        self.assertEqual(selected.selected_lane, 1)
        for name in ('fifo_empty', 'fifo_rd_en', 'fifo_pop_accepted',
                'fifo_q_valid_model', 'byte_phy_rden', 'dfi_read_command',
                'rd_input', 'read_valid', 'ready', 'control_phy_rden',
                'launch_ca'):
            self.assertEqual(selected.fields[name][1], full.fields[name][1])
        for name in ('dq_q', 'dfi_rddata', 'dq_bitslip_o'):
            self.assertEqual(selected.fields[name][1], 64)
        self.assertEqual(selected.fields['dq_bitslip_state'][1], 24)

    def test_selected_physical_dfi_lane_is_packed_in_outer_ui_order(self):
        phases = [SimpleNamespace(rddata=Signal(128)) for _ in range(4)]
        selected = Signal(64)
        dut = Module()
        dut.comb += selected.eq(logical_dfi_lane_words(phases, 1))

        def run():
            phase_words = []
            expected = 0
            for phase in range(4):
                word = 0
                for half in range(2):
                    ui = phase*2 + half
                    value = 0x40 + ui
                    word |= value << (half*64 + 8)
                    expected |= value << (ui*8)
                phase_words.append(word)
            for phase, word in zip(phases, phase_words):
                yield phase.rddata.eq(word)
            yield
            self.assertEqual((yield selected), expected)

        run_simulation(dut, run())

    def test_selected_trace_lane_validation(self):
        for lane in (-1, 8, True, 1.0):
            with self.assertRaises(ValueError):
                NativeRXTraceLayout(databits=64, ntaps=106,
                    dfi_data_width=512, selected_lane=lane)
        phases = [SimpleNamespace(rddata=Signal(128)) for _ in range(4)]
        for lane in (-1, 8, True):
            with self.assertRaises(ValueError):
                logical_dfi_lane_words(phases, lane)

    def test_boundary_trace_freezes_exact_pretrigger_and_posttrigger_window(self):
        dut = NativeRXBoundaryTrace(payload_width=9)

        def run():
            for cycle in range(50):
                yield dut.sample.eq(cycle | ((cycle == 27) << 8))
                yield dut.mark.eq(cycle == 20)
                yield dut.lane_pop.eq(cycle == 27)
                yield
            self.assertEqual((yield dut.done), 1)
            self.assertEqual((yield dut.pre_samples), 16)
            self.assertEqual((yield dut.mark_seen), 1)
            self.assertEqual((yield dut.trigger_seen), 1)
            self.assertEqual((yield dut.trigger_timeout), 0)
            samples = []
            for index in range(32):
                yield dut.sample_index.eq(index)
                yield
                captured = (yield dut.data)
                samples.append(captured)
            payloads = [sample & 0x1ff for sample in samples]
            self.assertEqual([item & 0xff for item in payloads],
                list(range(payloads[0] & 0xff, (payloads[0] & 0xff) + 32)))
            self.assertEqual([i for i, item in enumerate(payloads) if item >> 8], [16])
            timestamps = [sample >> 9 for sample in samples]
            self.assertEqual(timestamps, list(range(timestamps[0], timestamps[0] + 32)))
            yield dut.sample.eq(0xee)
            yield dut.mark.eq(0)
            yield dut.lane_pop.eq(0)
            yield
            self.assertEqual((yield dut.done), 1)
            yield dut.sample_index.eq(0)
            yield
            self.assertEqual((yield dut.data), samples[0])

        run_simulation(dut, run())

    def test_boundary_trace_layout_exposes_lane_fields_and_cycle_counter(self):
        layout = NativeRXBoundaryTraceLayout()
        values = {
            "read_request": 1,
            "control_phy_rden": 0xa5,
            "lane_fifo_empty": 0x12,
            "lane_fifo_rd_en": 0x34,
            "lane_fifo_accepted": 0x56,
            "dq_raw": 0x89abcdef,
            "dq_returned": 0x01234567,
            "read_valid": 1,
            "repeat_mark": 1,
            "cycle_count": 0xcdef,
        }
        sample = sum(value << layout.fields[name][0]
                     for name, value in values.items())
        self.assertEqual(layout.decode(sample), values)
        self.assertEqual((layout.width + 31)//32, 4)

    def test_boundary_raw_dq_uses_logical_indices_with_nonidentity_tap_map(self):
        # The real logical DQ12..15 taps are [23, 24, 21, 22]. The adapter
        # bus is already reordered, so raw trace data must select logical
        # indices 12..15 rather than those physical tap numbers.
        dq_taps = [23, 24, 21, 22]
        logical_bits = range(12, 16)
        logical_rx_data = Signal(64*8)
        # Build an explicit output signal so this is an elaborated mux/slice
        # test, not only a Python-list arithmetic check.
        output = Signal(32)
        dut = Module()
        dut.comb += output.eq(logical_dq_words(logical_rx_data, logical_bits))

        def run():
            payload = sum(((0x80 + bit) & 0xff) << (8*bit) for bit in range(64))
            yield logical_rx_data.eq(payload)
            yield
            observed = (yield output)
            expected = sum((0x80 + bit) << (8*index)
                for index, bit in enumerate(logical_bits))
            wrong_tap_indexed = sum((0x80 + tap) << (8*index)
                for index, tap in enumerate(dq_taps))
            self.assertEqual(observed, expected)
            self.assertNotEqual(observed, wrong_tap_indexed)

        run_simulation(dut, run())

    def test_boundary_trace_reports_missing_lane_pop_after_mark_timeout(self):
        dut = NativeRXBoundaryTrace(payload_width=8)

        def run():
            yield dut.mark.eq(1)
            yield
            yield dut.mark.eq(0)
            for _ in range(4100):
                yield
            self.assertEqual((yield dut.mark_seen), 1)
            self.assertEqual((yield dut.trigger_seen), 0)
            self.assertEqual((yield dut.trigger_timeout), 1)
            self.assertEqual((yield dut.done), 0)

        run_simulation(dut, run())

    def test_read_valid_snapshot_holds_first_beat_and_fifo_state(self):
        # Pack four 16-bit DFI phases, then four-bit FIFO empty and read-enable
        # vectors. A later beat and FIFO drain must not replace the first edge.
        dut = NativeReadValidSnapshot(width=4*16 + 2*4)
        first = (0x1234 | (0x5678 << 16) | (0x9abc << 32) |
            (0xdef0 << 48) | (0b0101 << 64) | (0b1010 << 68))
        second = (0xaaaa | (0xbbbb << 16) | (0xcccc << 32) |
            (0xdddd << 48) | (0b1111 << 64) | (0b0000 << 68))

        def run():
            yield dut.arm.eq(1)
            yield
            yield dut.arm.eq(0)
            yield dut.data.eq(first)
            yield dut.read_valid.eq(1)
            yield
            yield
            self.assertEqual((yield dut.done), 1)
            self.assertEqual((yield dut.snapshot), first)

            # Subsequent valid data and changing FIFO state model later
            # returns and software draining the FIFOs after the edge.
            yield dut.read_valid.eq(0)
            yield dut.data.eq(second)
            yield
            yield dut.read_valid.eq(1)
            yield
            yield
            self.assertEqual((yield dut.snapshot), first)
            self.assertEqual((yield dut.done), 1)

        run_simulation(dut, run())

    def test_fifo_acceptance_and_modeled_q_valid_latency(self):
        dut = NativeRXFIFOStatusTrace(ntaps=4)

        def run():
            yield dut.rd_en.eq(0b1111)
            yield dut.empty.eq(0b1010)
            yield
            self.assertEqual((yield dut.accepted), 0b0101)
            self.assertEqual((yield dut.q_valid_model), 0)
            yield
            self.assertEqual((yield dut.q_valid_model), 0b0101)
            yield dut.rd_en.eq(0b1000)
            yield dut.empty.eq(0)
            yield
            self.assertEqual((yield dut.accepted), 0b1000)
            self.assertEqual((yield dut.q_valid_model), 0b0101)
            yield
            self.assertEqual((yield dut.q_valid_model), 0b1000)

        run_simulation(dut, run())

    def test_lane_pop_scoreboard_accumulates_all_dq_pops_per_read(self):
        dut = NativeRXLanePopScoreboard(16, (tuple(range(8)), tuple(range(8, 16))))

        def run():
            # Each lane's eight DQ FIFOs may be accepted on different cycles.
            for tap in range(16):
                yield dut.accepted.eq(1 << tap)
                yield
            yield dut.accepted.eq(0)
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield
            self.assertEqual((yield dut.fault), 0)
            self.assertEqual((yield dut.missing_lanes), 0)
            self.assertEqual((yield dut.completed_reads), 1)

            # A following read with all lane FIFOs accepted also passes.
            yield dut.accepted.eq(0xffff)
            yield
            yield dut.accepted.eq(0)
            yield
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield
            self.assertEqual((yield dut.fault), 0)
            self.assertEqual((yield dut.completed_reads), 2)

            # One missing DQ FIFO marks the containing byte lane as incomplete.
            yield dut.accepted.eq(0xff7f)
            yield
            yield dut.accepted.eq(0)
            yield
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield
            self.assertEqual((yield dut.fault), 1)
            self.assertEqual((yield dut.missing_lanes), 0b01)
            self.assertEqual((yield dut.completed_reads), 3)

            yield dut.clear.eq(1)
            yield
            yield dut.clear.eq(0)
            yield
            self.assertEqual((yield dut.fault), 0)
            self.assertEqual((yield dut.missing_lanes), 0)
            self.assertEqual((yield dut.completed_reads), 0)

        run_simulation(dut, run())

    def test_lane_pop_scoreboard_checks_each_valid_even_without_pops(self):
        dut = NativeRXLanePopScoreboard(8, (tuple(range(8)),))

        def run():
            yield dut.read_valid.eq(1)
            yield
            yield dut.read_valid.eq(0)
            yield
            self.assertEqual((yield dut.fault), 1)
            self.assertEqual((yield dut.missing_lanes), 1)
            self.assertEqual((yield dut.completed_reads), 1)

        run_simulation(dut, run())

    def test_lane_pop_scoreboard_includes_acceptance_on_valid_cycle(self):
        dut = NativeRXLanePopScoreboard(8, (tuple(range(8)),))

        def run():
            yield dut.accepted.eq(0xff)
            yield dut.read_valid.eq(1)
            yield
            yield dut.accepted.eq(0)
            yield dut.read_valid.eq(0)
            yield
            yield
            self.assertEqual((yield dut.fault), 0)
            self.assertEqual((yield dut.missing_lanes), 0)
            self.assertEqual((yield dut.completed_reads), 1)

        run_simulation(dut, run())

    def test_sample_layout_and_decode(self):
        # One lane has eight DQ pins; there are four 32-bit DFI phases and
        # the two tap-indexed status buses precede the command/valid flags.
        layout = NativeRXTraceLayout(databits=8, ntaps=13,
            dfi_data_width=4*32)
        sample = layout.pack(
            dq_q=0x0123456789abcdef,
            dfi_rddata=0x76543210fedcba98,
            fifo_empty=0x1234,
            fifo_rd_en=0x15a3,
            fifo_pop_accepted=0x0123,
            fifo_q_valid_model=0x0456,
            dq_bitslip_o=0x1122334455667788,
            dq_bitslip_state=0xabcdef,
            byte_phy_rden=0x1,
            dfi_read_command=1,
            rd_input=1, read_valid=0, ready=1)
        decoded = layout.decode(sample)
        self.assertEqual(decoded["dq_q"], 0x0123456789abcdef)
        self.assertEqual(decoded["dfi_rddata"], 0x76543210fedcba98)
        self.assertEqual(decoded["fifo_empty"], 0x1234)
        self.assertEqual(decoded["fifo_rd_en"], 0x15a3)
        self.assertEqual(decoded["fifo_pop_accepted"], 0x0123)
        self.assertEqual(decoded["fifo_q_valid_model"], 0x0456)
        self.assertEqual(decoded["dq_bitslip_o"], 0x1122334455667788)
        self.assertEqual(decoded["dq_bitslip_state"], 0xabcdef)
        self.assertEqual(decoded["byte_phy_rden"], 0x1)
        self.assertEqual(decoded["dfi_read_command"], 1)
        self.assertEqual(decoded["rd_input"], 1)
        self.assertEqual(decoded["read_valid"], 0)
        self.assertEqual(decoded["ready"], 1)
        self.assertEqual(layout.fields["fifo_empty"][0], 8*8 + 4*32)
        self.assertEqual(layout.fields["fifo_rd_en"][0], 8*8 + 4*32 + 13)
        extras = 2*13 + 8*8 + 3*8
        self.assertEqual(layout.fields["byte_phy_rden"][0], 8*8 + 4*32 + 2*13 + extras)
        self.assertEqual(layout.fields["dfi_read_command"][0], 8*8 + 4*32 + 2*13 + extras + 1)
        self.assertEqual(layout.width, 8*8 + 4*32 + 2*13 + extras + 1 + 4)

    def test_full_control_gate_masks_survive_trace_packing(self):
        layout = NativeRXTraceLayout(databits=8, ntaps=13,
            dfi_data_width=4*32, ncontrols=2)
        sample = layout.pack(control_phy_rden=0xa5, rd_input=1)
        decoded = layout.decode(sample)
        self.assertEqual(decoded["control_phy_rden"], 0xa5)
        self.assertEqual(decoded["rd_input"], 1)
        self.assertEqual(layout.fields["control_phy_rden"][1], 8)

        command_layout = NativeRXTraceLayout(databits=8, ntaps=13,
            dfi_data_width=4*32, ncontrols=2, ca_width=40)
        command = 0xff_ff_ff_00_00  # CS/CAS active across all eight slots.
        decoded = command_layout.decode(command_layout.pack(
            control_phy_rden=0xa5, launch_ca=command))
        self.assertEqual(decoded["launch_ca"], command)
        self.assertEqual(decoded["control_phy_rden"], 0xa5)

        monitored = NativeRXTraceLayout(databits=8, ntaps=13,
            dfi_data_width=4*32, ncontrols=2, ca_width=40,
            dqs_counter_lanes=2)
        counters = (0x87654321 << 32) | 0x12345678
        decoded = monitored.decode(monitored.pack(
            control_phy_rden=0xa5, launch_ca=command,
            dqs_wrclk_edges=counters))
        self.assertEqual(decoded["dqs_wrclk_edges"], counters)
        self.assertEqual(monitored.width - command_layout.width, 64)

        mrs_layout = NativeRXTraceLayout(databits=8, ntaps=13,
            dfi_data_width=4*32, ncontrols=2, ca_width=40,
            mrs_address_width=24, dqs_counter_lanes=2)
        # Serial A2, BA0 and BA1 occupy consecutive eight-slot fields.
        mrs_address = 0x06ffff
        decoded = mrs_layout.decode(mrs_layout.pack(
            launch_ca=command, launch_mrs_address=mrs_address,
            dqs_wrclk_edges=counters))
        self.assertEqual(decoded["launch_mrs_address"], mrs_address)
        self.assertEqual(decoded["dqs_wrclk_edges"], counters)
        self.assertEqual(mrs_layout.width - monitored.width, 24)

        dm_clocks = NativeRXTraceLayout(databits=8, ntaps=13,
            dfi_data_width=4*32, ncontrols=2, ca_width=40,
            mrs_address_width=24, dqs_counter_lanes=2,
            dm_counter_lanes=2)
        edge_counts = (2 << 32) | 1
        decoded = dm_clocks.decode(dm_clocks.pack(
            launch_mrs_address=mrs_address, dqs_wrclk_edges=counters,
            dm_wrclk_edges=edge_counts))
        self.assertEqual(decoded["dm_wrclk_edges"], edge_counts)
        self.assertEqual(dm_clocks.width - mrs_layout.width, 64)

    def test_launched_gates_append_without_changing_old_offsets(self):
        kwargs = dict(databits=32, ntaps=65, dfi_data_width=256,
            ncontrols=12, ca_width=40, mrs_address_width=24,
            dqs_counter_lanes=4, dm_counter_lanes=2)
        old = NativeRXTraceLayout(**kwargs)
        extended = NativeRXTraceLayout(**kwargs, launched_gate_controls=12)
        for name, field in old.fields.items():
            self.assertEqual(extended.fields[name], field)
        self.assertEqual(extended.fields["control_phy_rden_launch"], (old.width, 48))
        packed = extended.pack(control_phy_rden_launch=0xfedcba987654,
            launch_mrs_address=0x060606, dm_wrclk_edges=(17 << 32) | 9)
        decoded = extended.decode(packed)
        self.assertEqual(decoded["control_phy_rden_launch"], 0xfedcba987654)
        self.assertEqual(decoded["dm_wrclk_edges"], (17 << 32) | 9)
        self.assertEqual(decoded["launch_mrs_address"], 0x060606)

    def test_trigger_alignment_and_rearm(self):
        dut = NativeRXTrace(32, depth=8)

        def run():
            for base in (0x100, 0x200):
                yield dut.arm.eq(1)
                yield
                yield dut.arm.eq(0)
                for _ in range(3):
                    yield
                self.assertEqual((yield dut.done), 0)
                yield dut.enable.eq(0)
                yield dut.trigger.eq(1)
                for _ in range(10):
                    yield
                self.assertEqual((yield dut.done), 0)
                yield dut.trigger.eq(0)
                yield dut.enable.eq(1)
                for cycle in range(12):
                    yield dut.sample.eq(base + cycle)
                    yield dut.trigger.eq(cycle == 0)
                    yield
                self.assertEqual((yield dut.done), 1)
                for index in range(8):
                    yield dut.index.eq(index)
                    for _ in range(3):
                        yield
                    self.assertEqual((yield dut.data), base + index)

        run_simulation(dut, run())

    def test_sample_trigger_boundary_attributes_survive_verilog_conversion(self):
        dut = NativeRXTrace(32, depth=8)
        source = verilog.convert(dut, ios={dut.arm, dut.trigger, dut.enable,
            dut.sample, dut.index, dut.data, dut.done, dut.busy}).main_source
        self.assertIn('keep = "true"', source)
        self.assertIn('dont_touch = "true"', source)
        self.assertIn('staged_sample', source)
        self.assertIn('staged_trigger', source)
