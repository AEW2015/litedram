#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest

from migen import Module, Signal
from migen.sim import run_simulation

from litedram.common import tXXDController


class TestRegisteredTimingValid(unittest.TestCase):
    def test_ready_schedule_matches_existing_controller(self):
        # Include closely spaced attempts, attempts during cooldown, and
        # idle periods. Controllers receive only accepted-command pulses.
        events = [0]*12 + [1, 0, 0, 1, 1, 0, 0, 0, 1] + [0]*12
        events += [int((cycle*17 + cycle//3) % 11 < 3) for cycle in range(96)]

        for delay in (1, 2, 3, 4, 7, 15):
            with self.subTest(delay=delay):
                dut = Module()
                dut.submodules.reference = tXXDController(delay)
                dut.submodules.registered = tXXDController(delay, registered_valid=True)
                request = Signal()
                dut.comb += [
                    dut.reference.valid.eq(request & dut.reference.ready),
                    dut.registered.valid.eq(request & dut.registered.ready),
                ]

                def stimulus():
                    for cycle, event in enumerate(events):
                        yield request.eq(event)
                        yield
                        self.assertEqual((yield dut.reference.ready),
                            (yield dut.registered.ready),
                            f"delay={delay} cycle={cycle}")

                run_simulation(dut, stimulus())

    def test_accepted_events_holdoff_through_backpressure(self):
        # A continuously asserted request models consecutive command attempts.
        # ``permit`` models the multiplexer selecting refresh or otherwise
        # applying backpressure. Only commands accepted while ready may start
        # the timer; readiness must fall immediately after that accepting edge.
        permit_pattern = [0, 0, 1, 1, 1, 0, 1, 0, 0, 1] * 8

        for delay in (2, 3, 4, 7):
            with self.subTest(delay=delay):
                dut = Module()
                dut.submodules.reference = tXXDController(delay)
                dut.submodules.registered = tXXDController(delay, registered_valid=True)
                request = Signal(reset=1)
                permit = Signal()
                dut.comb += [
                    dut.reference.valid.eq(request & permit & dut.reference.ready),
                    dut.registered.valid.eq(request & permit & dut.registered.ready),
                ]

                def stimulus():
                    for cycle, allowed in enumerate(permit_pattern):
                        yield permit.eq(allowed)
                        accepted = (yield dut.registered.valid)
                        yield
                        reference_ready = (yield dut.reference.ready)
                        registered_ready = (yield dut.registered.ready)
                        self.assertEqual(reference_ready, registered_ready,
                            f"delay={delay} cycle={cycle}")
                        if accepted:
                            self.assertEqual(registered_ready, 0,
                                f"delay={delay} accepted command was not blocked at cycle={cycle}")

                run_simulation(dut, stimulus())


if __name__ == "__main__":
    unittest.main()
