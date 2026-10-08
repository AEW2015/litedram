#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Independent timestamp checks for the global dual-slot timing gate."""
import unittest

from migen import ClockDomain, Module
from litex.gen.sim import run_simulation

from litedram.core.dual_slot_timing import DualSlotTimingAdmission


class TestDualSlotTimingAdmission(unittest.TestCase):
    def test_turnaround_from_later_slot_with_refresh_and_maintenance_gaps(self):
        # Floors are independently fixed here to make their timestamps clear:
        # WTR=max(8, 12+4+8)=24 CK; RTW=max(8, 16+4+2-12)=10 CK.
        dut = DualSlotTimingAdmission(cl=16, cwl=12, twtr=8,
                                      tccd_s=4, tccd_l=8)
        top = Module()
        top.clock_domains.cd_sys = ClockDomain()
        top.submodules.dut = dut
        observed = []

        def bench():
            # Pair of writes at CK0 and CK4. The WTR clock starts at CK4.
            yield dut.req_write[0].eq(1)
            yield dut.req_write[1].eq(1)
            yield dut.accepted[0].eq(1)
            yield dut.accepted[1].eq(1)
            yield
            yield dut.accepted[0].eq(0)
            yield dut.accepted[1].eq(0)
            yield dut.req_write[0].eq(0)
            yield dut.req_write[1].eq(0)

            # CK8, 16 and 24 are still too early for a read after CK4.
            for cycle in (1, 2):
                yield dut.refresh_inhibit.eq(cycle == 1)
                yield dut.maintenance_valid.eq(cycle == 2)
                yield
                self.assertEqual((yield dut.read_ready[0]), 0)
                self.assertEqual((yield dut.read_ready[1]), 0)
            yield dut.refresh_inhibit.eq(0)
            yield dut.maintenance_valid.eq(0)
            yield
            # At CK32 boundary only CK28 has elapsed; slot 1 at CK36 is safe.
            self.assertEqual((yield dut.read_ready[0]), 0)
            self.assertEqual((yield dut.read_ready[1]), 1)
            observed.append((28, "W"))
            yield dut.accepted[1].eq(1)
            yield
            yield dut.accepted[1].eq(0)
            yield dut.req_write[1].eq(1)

            # Read accepted at CK36. R->W floor is 10 CK, so CK44 is early;
            # CK48 is the first slow-cycle boundary that can safely issue.
            yield
            self.assertEqual((yield dut.write_ready[0]), 0)
            self.assertEqual((yield dut.write_ready[1]), 0)
            yield
            self.assertEqual((yield dut.write_ready[0]), 1)
            self.assertEqual((yield dut.write_ready[1]), 1)
            observed.append((48, "R"))
            yield dut.accepted[0].eq(1)
            yield
            yield dut.accepted[0].eq(0)

        run_simulation(top, bench())
        self.assertEqual(observed, [(28, "W"), (48, "R")])
        self.assertGreaterEqual(28 - 4, 24)
        self.assertGreaterEqual(48 - 36, 10)

    def test_floor_parameters_can_raise_derived_values(self):
        dut = DualSlotTimingAdmission(cl=12, cwl=10, twtr=4,
                                      write_to_read_ck=31,
                                      read_to_write_ck=17)
        top = Module()
        top.clock_domains.cd_sys = ClockDomain()
        top.submodules.dut = dut
        samples = []

        def bench():
            yield dut.req_write[0].eq(1)
            yield dut.accepted[0].eq(1)
            yield
            yield dut.accepted[0].eq(0)
            yield dut.req_write[0].eq(0)
            for _ in range(3):
                yield
                samples.append((yield dut.read_ready[0]))
            yield
            samples.append((yield dut.read_ready[0]))

        run_simulation(top, bench())
        self.assertEqual(samples, [0, 0, 0, 1])


if __name__ == "__main__":
    unittest.main()
