#
# SPDX-License-Identifier: BSD-2-Clause

"""Exercise NativeDMABenchmark against the production DDR4 1:8 controller."""

import unittest
from collections import Counter
from functools import reduce
from operator import or_

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.frontend.native_benchmark import NativeDMABenchmark
from litedram.frontend.native_diagnostics import LiteDRAMNativeDiagnostics
from litedram.modules import EDY4016A
from litedram.phy.utils import Deserializer, Serializer
from test.common import timeout_generator


def run_controller_simulation(dut, generators):
    # The pinned Migen simulator requires read wires on write-only FIFO ports.
    fragment = dut.get_fragment()
    for special in fragment.specials:
        if isinstance(special, Memory):
            for memory_port in special.ports:
                if memory_port.dat_r is None:
                    memory_port.dat_r = Signal(special.width)
    run_simulation(fragment, generators)


class TestNativeDMABenchmarkIntegration(unittest.TestCase):
    def _run_prbs_writer_case(self, beat_count, trefi_override=None):
        module = EDY4016A(150e6, "1:8", speedgrade="2400")
        if trefi_override is not None:
            module.timing_settings.tREFI = trefi_override
        phy = PhySettings(
            phytype="USNativeDDRPHY-audit", memtype="DDR4", databits=32,
            dfi_databits=32, nphases=8, rdphase=2, wrphase=3, cl=17,
            cwl=12, cmd_latency=5,
            read_latency=12//2 + Serializer.LATENCY + Deserializer.LATENCY,
            write_latency=3//2, nranks=1)
        controller = LiteDRAMController(
            phy, module.geom_settings, module.timing_settings, clk_freq=150e6,
            controller_settings=ControllerSettings(
                with_registered_row_hit=True,
                with_registered_refresh_timers=True,
                with_registered_refresh_request=True,
                with_registered_timing_valid=True,
                with_activate_eligibility=True))

        top = Module()
        top.submodules.controller = controller
        crossbar = LiteDRAMCrossbar(controller.interface)
        top.submodules.crossbar = crossbar
        write_port = crossbar.get_port(mode="write")
        read_port = crossbar.get_port(mode="read")
        beat_bytes = write_port.data_width // 8
        benchmark = NativeDMABenchmark(
            write_port, read_port, capacity=0x40000000, fifo_depth=64,
            databits=32)
        top.submodules.benchmark = benchmark
        diagnostics = LiteDRAMNativeDiagnostics()
        top.submodules.diagnostics = diagnostics

        bank_machines = [module for _, module in controller._submodules
                         if hasattr(module, "activate_ready")]
        bank_command_pending = Signal()
        cas_count = Signal(4)
        pending_terms = [bm.cmd.valid for bm in bank_machines]
        cas_terms = [~phase.cs_n & phase.ras_n & ~phase.cas_n
                     for phase in controller.dfi.phases]
        self_comb = [bank_command_pending.eq(reduce(or_, pending_terms)),
                     cas_count.eq(sum(cas_terms, Constant(0, 4))),
                     diagnostics.command_valid.eq(write_port.cmd.valid),
                     diagnostics.command_ready.eq(write_port.cmd.ready),
                     diagnostics.bank_command_pending.eq(bank_command_pending),
                     diagnostics.cas_count.eq(cas_count),
                     diagnostics.writer_fifo_full.eq(~benchmark.writer.fifo.sink.ready),
                     diagnostics.reader_reservation_full.eq(
                         benchmark.reader.enable & benchmark.reader.sink.valid &
                         read_port.cmd.ready & ~benchmark.reader.sink.ready)]
        top.comb += self_comb

        events = {"cycle": 0, "write_cas": [], "write_cmd_stall": 0,
                  "write_cmd_accept": 0, "write_data_accept": 0,
                  "refresh_commands": 0, "invalid_refresh_entries": [],
                  "diagnostic_counts": {}, "cas_details": [], "gap_samples": [],
                  "fsm_transitions": []}
        previous_fsm_state = [None]

        @passive
        def monitor():
            while True:
                cycle = events["cycle"]
                cas_seen = False
                for phase in controller.dfi.phases:
                    if ((yield phase.cs_n) == 0 and (yield phase.ras_n) == 1 and
                            (yield phase.cas_n) == 0 and (yield phase.we_n) == 0):
                        cas_seen = True
                        cas_detail = {
                            "cycle": cycle,
                            "index": len(events["write_cas"]),
                            "bank": (yield phase.bank),
                            "column": (yield phase.address),
                            "auto_precharge": bool((yield phase.address) & (1 << 10)),
                        }
                    if ((yield phase.cs_n) == 0 and (yield phase.ras_n) == 0 and
                            (yield phase.cas_n) == 0 and (yield phase.we_n) == 1):
                        events["refresh_commands"] += 1
                if cas_seen:
                    events["write_cas"].append(cycle)
                    events["cas_details"].append(cas_detail)
                elif events["write_cas"]:
                    gap = cycle - events["write_cas"][-1]
                    cas_index = len(events["write_cas"]) - 1
                    row_wrap_indices = list(range(127, 1024, 128)) + [1023]
                    row_wrap_sample = gap == 5 and cas_index in row_wrap_indices
                    refresh_sample = gap in (10, 100, 500, 1000, 1200) and cas_index >= 1024
                    if row_wrap_sample or refresh_sample:
                        bm_states = []
                        for bm in bank_machines:
                            bm_states.append({
                                "fsm": bm.fsm.decoding[(yield bm.fsm.state)],
                                "refresh_req": (yield bm.refresh_req),
                                "refresh_gnt": (yield bm.refresh_gnt),
                                "cmd_valid": (yield bm.cmd.valid),
                                "cmd_ready": (yield bm.cmd.ready),
                            })
                        events["gap_samples"].append({
                            "previous_cas": events["cas_details"][-1],
                            "age": gap,
                            "native_cmd_valid": (yield write_port.cmd.valid),
                            "native_cmd_ready": (yield write_port.cmd.ready),
                            "native_address": (yield write_port.cmd.addr),
                            "mux_fsm": controller.multiplexer.fsm.decoding[
                                (yield controller.multiplexer.fsm.state)],
                            "refresh_fsm": controller.refresher.fsm.decoding[
                                (yield controller.refresher.fsm.state)],
                            "refresh_cmd_valid": (yield controller.refresher.cmd.valid),
                            "bank_states": bm_states,
                        })
                if (yield write_port.cmd.valid) and (yield write_port.cmd.ready):
                    events["write_cmd_accept"] += 1
                if (yield write_port.cmd.valid) and not (yield write_port.cmd.ready):
                    events["write_cmd_stall"] += 1
                if (yield write_port.wdata.valid) and (yield write_port.wdata.ready):
                    events["write_data_accept"] += 1
                mux_state = controller.multiplexer.fsm.decoding[
                    (yield controller.multiplexer.fsm.state)]
                refresh_state = controller.refresher.fsm.decoding[
                    (yield controller.refresher.fsm.state)]
                fsm_state = (mux_state, refresh_state)
                if fsm_state != previous_fsm_state[0]:
                    bank_refresh_req = []
                    bank_refresh_gnt = []
                    for bm in bank_machines:
                        bank_refresh_req.append((yield bm.refresh_req))
                        bank_refresh_gnt.append((yield bm.refresh_gnt))
                    events["fsm_transitions"].append({
                        "cycle": cycle,
                        "cas_index": len(events["write_cas"]),
                        "native_address": (yield write_port.cmd.addr),
                        "native_cmd_valid": (yield write_port.cmd.valid),
                        "native_cmd_ready": (yield write_port.cmd.ready),
                        "mux_fsm": mux_state,
                        "refresh_fsm": refresh_state,
                        "refresh_cmd_valid": (yield controller.refresher.cmd.valid),
                        "bank_refresh_req": bank_refresh_req,
                        "bank_refresh_gnt": bank_refresh_gnt,
                    })
                    if (mux_state == "REFRESH" and
                            previous_fsm_state[0] is not None and
                            previous_fsm_state[0][0] != "REFRESH" and
                            not (yield controller.refresher.cmd.valid)):
                        events["invalid_refresh_entries"].append(cycle)
                    previous_fsm_state[0] = fsm_state
                events["cycle"] += 1
                yield

        def start_and_wait_write_phase():
            yield benchmark.allowed.eq(1)
            yield benchmark._random.storage.eq(1)  # PRBS31 pattern.
            yield benchmark._base.storage.eq(0)
            yield benchmark._length.storage.eq(beat_count * beat_bytes)
            yield benchmark._timeout.storage.eq(100000)
            yield benchmark._start.re.eq(1)
            yield
            yield benchmark._start.re.eq(0)
            for _ in range(100000):
                if (yield benchmark._write_beats.status) == beat_count:
                    events["diagnostic_counts"] = {
                        "cycles": (yield diagnostics._cycles.status),
                        "command_stall_cycles": (yield diagnostics._command_stall_cycles.status),
                        "bank_pending_cycles": (yield diagnostics._bank_command_pending_cycles.status),
                        "controller_cas_commands": (yield diagnostics._controller_cas_commands.status),
                        "writer_fifo_full_cycles": (yield diagnostics._writer_fifo_full_cycles.status),
                        "reader_reservation_full_cycles": (yield diagnostics._reader_reservation_full_cycles.status),
                    }
                    return
                yield
            self.fail("benchmark did not complete the write phase")

        run_controller_simulation(top, [start_and_wait_write_phase(), monitor(),
                                        timeout_generator(100000)])

        intervals = [b-a for a, b in zip(events["write_cas"], events["write_cas"][1:])]
        histogram = dict(sorted(Counter(intervals).items()))
        print("NativeDMABenchmark PRBS31 write diagnostic: beats={}, cycles={}, "
              "accepted_commands={}, accepted_write_data={}, command_stall_cycles={}, "
              "CAS={}, CAS_interval_histogram={}, counters={}, cas_details={}, "
                  "gap_samples={}, fsm_transitions={}".format(
                  beat_count, events["cycle"], events["write_cmd_accept"],
                  events["write_data_accept"], events["write_cmd_stall"],
                  len(events["write_cas"]), histogram, events["diagnostic_counts"],
                  events["cas_details"][-16:], events["gap_samples"],
                  events["fsm_transitions"]))
        self.assertEqual(events["write_cmd_accept"], beat_count)
        self.assertEqual(events["write_data_accept"], beat_count)
        self.assertEqual(len(events["write_cas"]), beat_count)
        return events, intervals

    def test_prbs_writer_steady_state_command_rate(self):
        self._run_prbs_writer_case(1050)

    def test_first_production_refresh_gap_state(self):
        self._run_prbs_writer_case(1250)

    def test_short_trefi_records_refresh_state_transitions(self):
        events, intervals = self._run_prbs_writer_case(192, trefi_override=100)
        self.assertGreater(events["refresh_commands"], 0)
        self.assertEqual(events["invalid_refresh_entries"], [])
        self.assertLess(max(intervals), 100)

    def test_diagnostic_counter_totals_and_clear(self):
        dut = LiteDRAMNativeDiagnostics()

        def generator():
            yield dut.command_valid.eq(1)
            yield dut.command_ready.eq(0)
            yield dut.bank_command_pending.eq(1)
            yield dut.cas_count.eq(2)
            yield dut.writer_fifo_full.eq(1)
            yield dut.reader_reservation_full.eq(1)
            yield
            yield
            yield dut.command_valid.eq(0)
            yield dut.command_ready.eq(1)
            yield dut.writer_fifo_full.eq(0)
            yield dut.reader_reservation_full.eq(0)
            yield
            self.assertEqual((yield dut._cycles.status), 3)
            self.assertEqual((yield dut._command_stall_cycles.status), 2)
            self.assertEqual((yield dut._bank_command_pending_cycles.status), 2)
            self.assertEqual((yield dut._controller_cas_commands.status), 4)
            self.assertEqual((yield dut._writer_fifo_full_cycles.status), 2)
            self.assertEqual((yield dut._reader_reservation_full_cycles.status), 2)
            yield dut.clear.eq(1)
            yield
            yield
            yield dut.clear.eq(0)
            self.assertEqual((yield dut._cycles.status), 0)
            self.assertEqual((yield dut._command_stall_cycles.status), 0)
            self.assertEqual((yield dut._bank_command_pending_cycles.status), 0)
            self.assertEqual((yield dut._controller_cas_commands.status), 0)
            self.assertEqual((yield dut._writer_fifo_full_cycles.status), 0)
            self.assertEqual((yield dut._reader_reservation_full_cycles.status), 0)

        run_simulation(dut, generator())

    def test_diagnostic_csr_clear_is_one_cycle_pulse(self):
        dut = LiteDRAMNativeDiagnostics()

        def generator():
            yield
            yield
            self.assertEqual((yield dut._cycles.status), 2)
            # A CSRStorage write leaves storage high. Only its write strobe
            # may clear the counters, or every subsequent count is lost.
            yield dut._clear.storage.eq(1)
            yield dut._clear.re.eq(1)
            yield
            yield dut._clear.re.eq(0)
            yield
            self.assertEqual((yield dut._cycles.status), 0)
            yield
            self.assertEqual((yield dut._cycles.status), 1)
            yield
            self.assertEqual((yield dut._cycles.status), 2)

        run_simulation(dut, generator())

