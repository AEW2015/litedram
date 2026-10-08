#
# Focused command-gap instrumentation for the wide DDR4 controller path.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest
from collections import Counter

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
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


class TestDMACommandGap(unittest.TestCase):
    def test_row_hit_stream_reports_command_gap_sources(self):
        """Measure CAS spacing and whether each idle cycle has queued work.

        This models the r28 eight phase x32 DDR4 DFI controller and continuously
        writes a sequential stream through row hits.  No PHY or
        hardware is involved.  The counters separate time with a command
        waiting inside a bank machine from time where the command path is
        empty while the native port is still trying to submit work.
        """
        module = EDY4016A(150e6, "1:8", speedgrade="2400")
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
        port = crossbar.get_port(mode="write")
        # Controller address is in 256-bit words.  One mode follows the DMA's
        # contiguous same-bank row-hit stream.
        count = 1026
        trace = {"cycle": 0, "cas": [], "idle": Counter(), "accepted": 0,
                 "source_stall": 0, "bank_accept": 0}
        bank_machines = [module for _, module in controller._submodules
                         if hasattr(module, "activate_ready")]

        @passive
        def monitor():
            while trace["cycle"] < 12000:
                cycle = trace["cycle"]
                cas = False
                for phase in controller.dfi.phases:
                    signals = ((yield phase.cas_n), (yield phase.ras_n), (yield phase.we_n))
                    if signals in ((0, 1, 1), (0, 1, 0)):
                        cas = True
                if cas:
                    trace["cas"].append(cycle)
                else:
                    pending = False
                    for bm in bank_machines:
                        pending |= (yield bm.cmd.valid)
                    source_waiting = (yield port.cmd.valid) and not (yield port.cmd.ready)
                    if pending:
                        trace["idle"]["bank_machine_command_pending"] += 1
                    elif source_waiting:
                        trace["idle"]["crossbar_or_bank_input_blocked"] += 1
                    else:
                        trace["idle"]["no_command_pending"] += 1
                if (yield port.cmd.valid) and (yield port.cmd.ready):
                    trace["accepted"] += 1
                if (yield port.cmd.valid) and not (yield port.cmd.ready):
                    trace["source_stall"] += 1
                for bank in range(len(bank_machines)):
                    interface_bank = getattr(controller.interface, "bank%d" % bank)
                    if (yield interface_bank.valid) and (yield interface_bank.ready):
                        trace["bank_accept"] += 1
                trace["cycle"] += 1
                yield

        def traffic():
            yield port.cmd.valid.eq(0)
            yield port.wdata.valid.eq(0)
            yield
            yield port.cmd.we.eq(1)
            yield port.cmd.valid.eq(1)
            yield port.cmd.addr.eq(0)
            yield port.wdata.valid.eq(1)
            yield port.wdata.we.eq((1 << len(port.wdata.we)) - 1)
            yield port.wdata.data.eq(0)
            accepted_commands = 0
            accepted_data = 0
            while accepted_commands < count:
                yield
                if (yield port.cmd.ready):
                    accepted_commands += 1
                    yield port.cmd.addr.eq(accepted_commands)
                if (yield port.wdata.ready):
                    accepted_data += 1
                    yield port.wdata.data.eq(accepted_data)
            yield port.cmd.valid.eq(0)
            while accepted_data < accepted_commands:
                yield
                if (yield port.wdata.ready):
                    accepted_data += 1
                    yield port.wdata.data.eq(accepted_data)
            yield port.wdata.valid.eq(0)
            # Let already accepted requests drain.
            for _ in range(200):
                yield

        run_controller_simulation(top, [traffic(), monitor(), timeout_generator(10000)])

        intervals = [b-a for a, b in zip(trace["cas"], trace["cas"][1:])]
        interval_histogram = dict(sorted(Counter(intervals).items()))
        print("DMA command-gap diagnostic: cycles={}, port_accepts={}, "
              "bank_accepts={}, source_stall_cycles={}, CAS={}, "
              "CAS_interval_histogram={}, idle_cycles={}".format(
                  trace["cycle"], trace["accepted"], trace["bank_accept"],
                  trace["source_stall"], len(trace["cas"]),
                  interval_histogram, dict(trace["idle"])))
        self.assertEqual(trace["accepted"], count)
        self.assertEqual(len(trace["cas"]), count)
        self.assertGreater(len(intervals), count // 2)
        self.assertGreaterEqual(intervals.count(1), count - 9)
        self.assertLessEqual(max(intervals), 12)

