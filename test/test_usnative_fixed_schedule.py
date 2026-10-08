#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Simulation-only fixed DFI schedule contract for native read assembly."""

import unittest
from migen import Memory, Signal
from migen.sim import run_simulation
from litedram.phy.usnative.elastic import NativeFIFOReceiveAdapter


def simulate(dut, process):
    fragment = dut.get_fragment()
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, process)


def layout(lanes):
    native_lanes = []
    slices = []
    for lane in range(lanes):
        base = lane * 10
        slices.extend(range(base, base + 10))
        native_lanes.append(type('Lane', (), dict(index=lane,
            dq=tuple(range(base, base + 8)), strobe=base + 8, mask=base + 9))())
    return type('Layout', (), dict(slices=tuple(slices), lanes=tuple(native_lanes)))()


class TestFixedDFIReadSchedule(unittest.TestCase):
    def test_prefilled_aligned_word_is_consumed_at_exact_command_latency(self):
        """The fixed schedule works only after its data-ready premise holds.

        The schedule token is independent of the adapter's elastic valid. This
        model waits for a complete word before accepting the command, then
        asserts the DFI boundary valid exactly LATENCY cycles after acceptance.
        """
        lanes, latency = 2, 5
        dut = NativeFIFOReceiveAdapter(layout(lanes), depth=8,
                                       response_latency=2)

        def process():
            yield dut.ready.eq(0)
            yield dut.lane_empty.eq(0)
            yield dut.lane_data[0].eq(0x101)
            yield dut.lane_data[1].eq(0x202)

            # Prime the assembler before issuing a command. Native response
            # latency, FIFO write latency and assembly must all fit before this
            # precondition can be claimed by a real controller.
            for _ in range(12):
                if (yield dut.valid):
                    break
                yield
            self.assertEqual((yield dut.valid), 1)

            command_cycle = 20
            due_cycle = command_cycle + latency
            observed_valid_cycles = []
            for cycle in range(due_cycle + 3):
                fixed_valid = cycle == due_cycle
                yield dut.ready.eq(fixed_valid)
                elastic_valid = (yield dut.valid)
                if fixed_valid:
                    self.assertEqual(elastic_valid, 1,
                        'scheduled DFI read underflowed: adapter had no complete word')
                    value = (yield dut.data)
                    self.assertEqual(value & ((1 << 64) - 1), 0x101)
                    self.assertEqual((value >> 64) & ((1 << 64) - 1), 0x202)
                    observed_valid_cycles.append(cycle)
                yield
            self.assertEqual(observed_valid_cycles, [command_cycle + latency])

        simulate(dut, process())

    def test_finite_skew_has_no_derived_fixed_latency_bound(self):
        """A schedule cannot be certified from queue depth and response latency.

        Lane 1 is withheld beyond the fixed-valid deadline. Reservation logic
        protects queue capacity, but cannot make the missing lane word arrive.
        A real integration must supply a measured/documented skew bound and
        show that all scheduled words are assembled before their DFI deadline.
        """
        lanes, latency = 2, 4
        dut = NativeFIFOReceiveAdapter(layout(lanes), depth=8,
                                       response_latency=2)
        deadline = latency

        def process():
            yield dut.ready.eq(0)
            yield dut.lane_data[0].eq(0x111)
            yield dut.lane_data[1].eq(0x222)
            for cycle in range(deadline + 3):
                # The late lane is unavailable through and beyond the deadline.
                yield dut.lane_empty.eq(0b10 if cycle <= deadline else 0)
                fixed_valid = cycle == deadline
                elastic_valid = (yield dut.valid)
                self.assertFalse(fixed_valid and elastic_valid,
                    'test premise should keep the assembled word unavailable at deadline')
                yield
            self.assertEqual((yield dut.valid), 0)

        simulate(dut, process())
