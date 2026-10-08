#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Full-rate DFI read-slot alignment at the DualSlotData return boundary."""

import unittest

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import LiteDRAMNativePort, TappedDelayLine
from litedram.core.dual_slot_data import DualSlotData
from litedram.phy.dfi import DFIRateConverter, Interface


class ReadPath(Module):
    def __init__(self, *, align_read_slots, read_latency):
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.clock_domains.cd_sys2x = ClockDomain("sys2x")
        self.fast = Interface(addressbits=17, bankbits=3, nranks=1,
                              databits=64, nphases=4)
        self.submodules.converter = DFIRateConverter(
            self.fast, clkdiv="sys", clk="sys2x", ratio=2,
            write_delay=1, read_delay=0, preserve_throughput=True,
            align_read_slots=align_read_slots, serdes_reset=ResetSignal("sys"))
        self.slow = self.converter.dfi

        self.ports = [LiteDRAMNativePort("both", 10, 256) for _ in range(2)]
        self.submodules.data = DualSlotData(
            self.ports, depth=8, read_latency=read_latency, write_latency=2)

        # Model the controller's registered command steerer and the PHY's
        # physical-CK read-valid/data latency. Acceptance is at the same
        # pre-steerer boundary that feeds DualSlotData.slot_valid.
        request = [Signal(name=f"slot{s}_request") for s in range(2)]
        tag = [Signal(16, name=f"slot{s}_tag") for s in range(2)]
        registered = [Signal(name=f"slot{s}_request_registered") for s in range(2)]
        registered_tag = [Signal(16, name=f"slot{s}_tag_registered") for s in range(2)]
        for slot, phase in enumerate((2, 6)):
            self.sync += [registered[slot].eq(request[slot]),
                          registered_tag[slot].eq(tag[slot])]
            self.comb += [self.slow.phases[phase].rddata_en.eq(registered[slot]),
                          self.slow.phases[phase].address.eq(registered_tag[slot])]
            self.comb += [self.data.slot_valid[slot].eq(request[slot]),
                          self.data.slot_write[slot].eq(0)]

        rd_en = self.fast.phases[2].rddata_en
        self.submodules.rd_valid_delay = ClockDomainsRenamer("sys2x")(
            TappedDelayLine(rd_en, ntaps=12))
        self.submodules.rd_tag_delay = ClockDomainsRenamer("sys2x")(
            TappedDelayLine(self.fast.phases[2].address, ntaps=12))
        for phase, phy_phase in enumerate(self.fast.phases):
            self.comb += [
                phy_phase.rddata_valid.eq(self.rd_valid_delay.output),
                phy_phase.rddata.eq((self.rd_tag_delay.output << 32) | phase),
            ]

        self.comb += [
            *[port.rdata.ready.eq(1) for port in self.ports],
            self.data.slot_rdata[0].eq(Cat(*[p.rddata for p in self.slow.phases[:4]])),
            self.data.slot_rdata[1].eq(Cat(*[p.rddata for p in self.slow.phases[4:8]])),
        ]
        self.request, self.tag = request, tag


class DualSlotConverterTest(unittest.TestCase):
    # AES-KU40 generates sys/sys2x from one MMCM with phase=0. LiteX's
    # simulator phase argument is the offset to the first transition; these
    # values make both clocks rise together (t=1, 9, 17, ...). The historical
    # (8, 1)/(4, 1) pair instead puts sys rising edges on sys2x falling edges.
    ALIGNED_CLOCKS = {"sys": (8, 3), "sys2x": (4, 1)}
    HALF_FAST_CLOCKS = {"sys": (8, 1), "sys2x": (4, 1)}
    # Every accepted command carries unique identity through both the PHY
    # latency model and the owner's response queue. Distinct owners are used
    # for paired slots, matching the crossbar's same-owner exclusion.
    sparse_issues = {
        3: ((10, 0), (11, 1)),
        5: ((12, 1), None),
        7: (None, (15, 0)),
        10: ((13, 0), (14, 1)),
        13: ((16, 1), None),
    }

    @staticmethod
    def expected_word(tag):
        return sum((((tag << 32) | phase) << (64 * phase))
                   for phase in range(4))

    def run_case(self, *, align, read_latency, issues=None, reset_cycles=2,
                 clock_phase="aligned"):
        if issues is None:
            issues = {cycle + reset_cycles: slots
                      for cycle, slots in self.sparse_issues.items()}
        dut = ReadPath(align_read_slots=align, read_latency=read_latency)
        observed = [[], []]
        errors = []
        horizon = max(issues, default=0) + 24

        def driver():
            for cycle in range(horizon):
                yield dut.cd_sys.rst.eq(int(cycle < reset_cycles))
                for slot in range(2):
                    event = issues.get(cycle, (None, None))[slot]
                    yield dut.request[slot].eq(event is not None)
                    yield dut.tag[slot].eq(0 if event is None else event[0])
                    yield dut.data.slot_owner[slot].eq(slot if event is None else event[1])
                yield

        def monitor():
            for cycle in range(horizon):
                for owner, port in enumerate(dut.ports):
                    if (yield port.rdata.valid):
                        response = (yield port.rdata.data)
                        observed[owner].append(response)
                yield
            errors.append((yield dut.data.error))

        fragment = dut.get_fragment()
        # The pinned simulator expects read wires on write-only FIFO memories.
        for special in fragment.specials:
            if isinstance(special, Memory):
                for port in special.ports:
                    if port.dat_r is None:
                        port.dat_r = Signal(special.width)
        clocks = self.ALIGNED_CLOCKS if clock_phase == "aligned" else self.HALF_FAST_CLOCKS
        run_simulation(fragment, {"sys": [driver(), monitor()]}, clocks=clocks)
        self.assertEqual(errors, [0])
        expected = [[], []]
        for cycle in sorted(issues):
            for slot in range(2):
                event = issues.get(cycle, (None, None))[slot]
                if event is not None:
                    tag, owner = event
                    expected[owner].append(self.expected_word(tag))
        return observed, expected

    def test_aligned_clocks_use_raw_deserializer_at_fixed_read_deadline(self):
        # The production PHY deadline is 9 sys cycles (read_latency 12 fast
        # cycles plus converter serializer/deserializer latency), followed by
        # one registered command-steerer cycle.
        raw, expected = self.run_case(align=False, read_latency=10)
        self.assertEqual(raw, expected)

        # The extra slow register on slot 1 is wrong for phase-aligned clocks:
        # it makes the upper paired beat stale at the controller boundary.
        delayed, _ = self.run_case(align=True, read_latency=10)
        self.assertNotEqual(delayed, expected)

    def test_aligned_read_deadline_boundaries(self):
        expected = [[], []]
        for cycle in sorted(self.sparse_issues):
            for slot in range(2):
                event = self.sparse_issues[cycle][slot]
                if event is not None:
                    tag, owner = event
                    expected[owner].append(self.expected_word(tag))
        for latency in (9, 11):
            with self.subTest(read_latency=latency):
                raw, _ = self.run_case(align=False, read_latency=latency)
                self.assertNotEqual(raw, expected)

    def test_half_fast_phase_preserves_historical_alignment_behavior(self):
        # This reproduces the old testbench phase relation and demonstrates
        # that it selects the opposite slot register setting from hardware.
        aligned, expected = self.run_case(
            align=True, read_latency=10, clock_phase="half_fast")
        self.assertEqual(aligned, expected)
        raw, _ = self.run_case(
            align=False, read_latency=10, clock_phase="half_fast")
        self.assertNotEqual(raw, expected)

    def test_lower_slot_startup_then_delayed_upper_slot_consecutive_reads(self):
        # Model open-row startup skew with three lower-slot-only accepted
        # cycles, followed by consecutive paired reads. Every beat has a
        # distinct tag and owner so a stale or misrouted upper response shows.
        issues = {
            3: ((200, 0), None),
            4: ((201, 1), None),
            5: ((202, 0), None),
        }
        for offset in range(12):
            issues[6 + offset] = ((300 + 2 * offset, offset % 2),
                                 (301 + 2 * offset, 1 - (offset % 2)))
        observed, expected = self.run_case(
            align=False, read_latency=10, issues=issues)
        self.assertEqual(observed, expected)

    def test_saturated_pairs_and_single_slot_runs_across_reset_release(self):
        # Four paired cycles, two slot-0-only cycles, then two slot-1-only
        # cycles repeat without gaps. Owners swap each cycle to stress routing.
        for reset_cycles in range(1, 8):
            with self.subTest(reset_release=reset_cycles):
                start = reset_cycles + 2
                issues = {}
                next_tag = 100
                for offset in range(24):
                    mode = offset % 8
                    owner0 = offset % 2
                    owner1 = 1 - owner0
                    slots = [None, None]
                    if mode < 4:
                        slots = [(next_tag, owner0), (next_tag + 1, owner1)]
                        next_tag += 2
                    elif mode < 6:
                        slots[0] = (next_tag, owner0)
                        next_tag += 1
                    else:
                        slots[1] = (next_tag, owner1)
                        next_tag += 1
                    issues[start + offset] = tuple(slots)
                observed, expected = self.run_case(
                    align=False, read_latency=10, issues=issues,
                    reset_cycles=reset_cycles)
                self.assertEqual([len(x) for x in observed],
                                 [len(x) for x in expected])
                self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()
