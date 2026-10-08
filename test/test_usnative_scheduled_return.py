#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Cycle tests for the diagnostic native FIFO return capture helper."""

import unittest
from itertools import count

from migen import Module, Mux, Signal
from migen.sim import run_simulation
import litex.soc.interconnect.csr as csr_module
import litedram.dfii as dfii_module

from litedram.dfii import PhaseInjector
from litedram.phy.dfi import Interface
from litedram.phy.usnative.ddrphy import NativeRXBitslip
from litedram.phy.usnative.scheduled_return import ScheduledNativeReturn


class ScheduledReturnHarness(Module):
    def __init__(self, lanes=4, width=64, bitslip_path=False):
        self.submodules.dut = ScheduledNativeReturn(lanes=lanes, data_width=width)
        self.phase = Interface(addressbits=1, bankbits=1, nranks=1,
                               databits=width, nphases=1).phases[0]
        self.ready = Signal(reset=1)
        self.reset = Signal()
        self.timeout = Signal()
        self.cancel_return_reset = Signal()
        self.cancel_return_ready = Signal()
        self.cancel_return_timeout = Signal()
        self.empty_idle = Signal(lanes)
        self.empty_at_pop = Signal(lanes)
        self.use_scheduled = Signal(reset=1)
        self.fixed_valid = Signal()
        # PhaseInjector's AutoCSR fields normally get names from the target
        # source AST. Supply harmless unique names for this direct simulation
        # construction, without changing the PhaseInjector logic.
        original_name_helper = csr_module.get_obj_var_name
        original_csr = dfii_module.CSR
        names = count()
        csr_module.get_obj_var_name = lambda name: name or "sim_csr_{}".format(next(names))
        class SimCSR(original_csr):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                # LiteX revisions differ on whether a standalone CSR exposes
                # the bus write strobe directly. PhaseInjector uses it only
                # for command issuance; these tests exercise its read latch.
                self.wr_stb = Signal()
        try:
            dfii_module.CSR = SimCSR
            self.submodules.phase_injector = PhaseInjector(self.phase)
        finally:
            dfii_module.CSR = original_csr
            csr_module.get_obj_var_name = original_name_helper
        self.comb += [
            self.dut.ready.eq(self.ready &
                ~(self.cancel_return_ready & self.dut.return_pending)),
            self.dut.reset.eq(self.reset |
                (self.cancel_return_reset & self.dut.return_pending)),
            self.dut.timeout.eq(self.timeout |
                (self.cancel_return_timeout & self.dut.return_pending)),
            self.dut.fifo_empty.eq(Mux(self.dut.rd_en,
                                       self.empty_at_pop, self.empty_idle)),
            self.phase.rddata_valid.eq(Mux(self.use_scheduled,
                                            self.dut.valid, self.fixed_valid)),
        ]
        if bitslip_path:
            self.raw_q = Signal(8)
            self.rx_slip = Signal()
            self.rx = NativeRXBitslip(i=self.raw_q, rst=0, slp=self.rx_slip,
                captured_word=self.dut.captured_word, captured_select=self.use_scheduled)
            self.submodules.rx = self.rx
            self.comb += self.phase.rddata.eq(self.rx.o)
        else:
            self.comb += self.phase.rddata.eq(self.dut.captured_word)


class ScheduledNativeReturnTest(unittest.TestCase):
    def test_native_bitslip_reads_captured_front_without_extra_sampling_cycle(self):
        harness = ScheduledReturnHarness(lanes=1, width=8, bitslip_path=True)
        dut = harness.dut
        front = 0x96
        observed = []

        def driver():
            yield harness.empty_idle.eq(0)
            yield harness.empty_at_pop.eq(0)
            yield harness.rx_slip.eq(1)
            yield
            yield harness.rx_slip.eq(0)
            yield
            yield dut.delay.eq(0)
            yield dut.fifo_front.eq(front)
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            while not (yield dut.rd_en):
                yield
            yield
            # Model Q being cleared immediately after the consume edge. The
            # held word drives the existing per-DQ rotation directly.
            yield dut.fifo_front.eq(0)
            yield harness.raw_q.eq(0)
            for _ in range(3):
                yield

        def monitor():
            yield "passive"
            while True:
                yield
                observed.append(((yield dut.valid),
                                 (yield harness.phase_injector._rddata.status)))

        run_simulation(harness, [driver(), monitor()])
        self.assertTrue(any(valid for valid, _ in observed))
        self.assertEqual(observed[-1][1], ((front >> 1) | (front << 7)) & 0xff)

    def test_unselected_helper_preserves_fixed_valid_and_normal_q_sampler(self):
        harness = ScheduledReturnHarness(lanes=1, width=8, bitslip_path=True)
        observed = []

        def driver():
            yield harness.use_scheduled.eq(0)
            yield harness.raw_q.eq(0x69)
            yield
            yield harness.raw_q.eq(0)
            yield harness.fixed_valid.eq(1)
            yield
            yield harness.fixed_valid.eq(0)
            for _ in range(2):
                yield

        def monitor():
            yield "passive"
            while True:
                yield
                observed.append((yield harness.phase_injector._rddata.status))

        run_simulation(harness, [driver(), monitor()])
        self.assertEqual(observed[-1], 0x69)

    def test_delay_endpoints_capture_front_and_phaseinjector_latches_next_edge(self):
        for delay in (0, 31):
            with self.subTest(delay=delay):
                harness = ScheduledReturnHarness()
                dut = harness.dut
                initial_word = 0xFEDCBA9876543210
                observed = []

                def driver():
                    yield dut.delay.eq(delay)
                    yield harness.empty_idle.eq(0)
                    yield harness.empty_at_pop.eq(0)
                    yield dut.fifo_front.eq(initial_word)
                    yield dut.read_request.eq(1)
                    yield
                    yield dut.read_request.eq(0)
                    # Wait through the registered launch, then model the
                    # front-visible FIFO advancing to EMPTY/zero after its
                    # consume edge. The helper must already have captured Q.
                    while not (yield dut.rd_en):
                        yield
                    yield
                    yield dut.fifo_front.eq(0)
                    yield harness.empty_idle.eq(0b1111)
                    for _ in range(4):
                        yield

                def monitor():
                    yield "passive"
                    while True:
                        yield
                        observed.append({
                            "accepted": (yield dut.request_accepted),
                            "rd_en": (yield dut.rd_en),
                            "valid": (yield dut.valid),
                            "captured": (yield dut.captured_word),
                            "status": (yield harness.phase_injector._rddata.status),
                        })

                run_simulation(harness, [driver(), monitor()])
                accepted = [i for i, s in enumerate(observed) if s["accepted"]]
                launches = [i for i, s in enumerate(observed) if s["rd_en"]]
                valids = [i for i, s in enumerate(observed) if s["valid"]]
                self.assertEqual(len(accepted), 1)
                self.assertEqual(len(launches), 1)
                self.assertEqual(len(valids), 1)
                self.assertEqual(launches[0] - accepted[0], delay + 1)
                # Pop/capture occurs one edge after RD_EN launch; valid then
                # persists through the next edge where PhaseInjector latches.
                self.assertEqual(valids[0] - launches[0], 1)
                self.assertEqual(observed[valids[0]]["captured"], initial_word)
                self.assertEqual(observed[valids[0] + 1]["status"], initial_word)

    def test_any_missing_lane_suppresses_valid_and_reports_actual_pop(self):
        harness = ScheduledReturnHarness()
        dut = harness.dut
        observed = []

        def driver():
            yield dut.delay.eq(0)
            yield dut.fifo_front.eq(0x123456789abcdef0)
            yield harness.empty_idle.eq(0)
            yield harness.empty_at_pop.eq(0b0100)
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            while not (yield dut.rd_en):
                yield
            for _ in range(4):
                yield

        def monitor():
            yield "passive"
            while True:
                yield
                observed.append(((yield dut.valid), (yield dut.underflow),
                                 (yield dut.missing_lanes),
                                 (yield harness.phase_injector._rddata.status)))

        run_simulation(harness, [driver(), monitor()])
        self.assertFalse(any(valid for valid, _, _, _ in observed))
        reports = [(missing, status) for _, underflow, missing, status in observed if underflow]
        self.assertEqual(reports, [(0b0100, 0)])

    def test_overlaps_are_rejected_through_phaseinjector_capture(self):
        harness = ScheduledReturnHarness(lanes=1)
        dut = harness.dut
        overlaps = []
        accepted = []

        def driver():
            yield dut.delay.eq(0)
            yield harness.empty_idle.eq(0)
            yield harness.empty_at_pop.eq(0)
            yield dut.fifo_front.eq(0xA5)
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            # Request again in the RD_EN-high cycle, so it overlaps the
            # actual FIFO-pop edge while the original remains outstanding.
            while not (yield dut.rd_en):
                yield
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            for _ in range(3):
                yield

        def monitor():
            yield "passive"
            while True:
                yield
                overlaps.append((yield dut.overlap))
                accepted.append((yield dut.request_accepted))

        run_simulation(harness, [driver(), monitor()])
        self.assertEqual(sum(overlaps), 1)
        self.assertEqual(sum(accepted), 1)

    def test_reset_ready_and_timeout_cancel_pending_and_returned_valid(self):
        for cancel_kind in ("reset", "ready", "timeout"):
            with self.subTest(cancel_kind=cancel_kind):
                harness = ScheduledReturnHarness(lanes=1)
                dut = harness.dut
                seen = []

                def driver():
                    yield dut.delay.eq(0)
                    yield harness.empty_idle.eq(0)
                    yield harness.empty_at_pop.eq(0)
                    yield dut.fifo_front.eq(0x5A)
                    # Arm a combinational cancel that activates only while a
                    # returned word is pending. This models reset/disable/
                    # timeout already asserted before the DFI sample edge.
                    if cancel_kind == "reset":
                        yield harness.cancel_return_reset.eq(1)
                    elif cancel_kind == "ready":
                        yield harness.cancel_return_ready.eq(1)
                    else:
                        yield harness.cancel_return_timeout.eq(1)
                    yield dut.read_request.eq(1)
                    yield
                    yield dut.read_request.eq(0)
                    while not (yield dut.rd_en):
                        yield
                    yield
                    yield harness.cancel_return_reset.eq(0)
                    yield harness.cancel_return_ready.eq(0)
                    yield harness.cancel_return_timeout.eq(0)
                    for _ in range(2):
                        yield

                def monitor():
                    yield "passive"
                    while True:
                        yield
                        seen.append(((yield dut.valid),
                                     (yield harness.phase_injector._rddata.status)))

                run_simulation(harness, [driver(), monitor()])
                self.assertTrue(any(not valid for valid, _ in seen))
                self.assertTrue(all(status == 0 for _, status in seen))

    def test_late_data_after_timeout_cannot_complete_expired_request(self):
        harness = ScheduledReturnHarness(lanes=1)
        dut = harness.dut
        accepted = []
        valid = []

        def driver():
            yield dut.delay.eq(31)
            yield dut.read_request.eq(1)
            yield
            yield dut.read_request.eq(0)
            # Abort while the delayed request is pending.
            yield harness.timeout.eq(1)
            yield
            yield harness.timeout.eq(0)
            # A late stale FIFO word arrives, but no request remains to pop it.
            yield dut.fifo_front.eq(0xDEADBEEFCAFEBABE)
            yield harness.empty_idle.eq(0)
            yield harness.empty_at_pop.eq(0)
            yield dut.read_request.eq(0)
            for _ in range(36):
                yield

        def monitor():
            yield "passive"
            while True:
                yield
                accepted.append((yield dut.request_accepted))
                valid.append((yield dut.valid))

        run_simulation(harness, [driver(), monitor()])
        self.assertEqual(sum(accepted), 1)
        self.assertFalse(any(valid))


if __name__ == "__main__":
    unittest.main()
