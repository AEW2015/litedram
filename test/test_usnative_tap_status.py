#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Tap-status selection and freshness after the documented settling interval."""

import unittest
from migen import *
from migen.sim import run_simulation
from migen.genlib.cdc import PulseSynchronizer
from litedram.phy.usnative.tap_status import (
    CSRStatusBridge, RegisteredTapStatus, TapCommandEvents,
)


class TapCommandEventsDUT(Module):
    def __init__(self):
        self.csr_commands = Signal(4)
        self.allowed = Signal()
        self.events = TapCommandEvents()
        self.status = RegisteredTapStatus(1)
        self.submodules.events = self.events
        self.submodules.status = self.status

        synchronized = []
        for bit in range(4):
            transfer = PulseSynchronizer("csr", "sys")
            self.submodules += transfer
            self.comb += transfer.i.eq(self.csr_commands[bit])
            synchronized.append(transfer.o)
        self.comb += [
            self.events.commands.eq(Cat(*synchronized)),
            self.events.allowed.eq(self.allowed),
            self.status.change.eq(self.events.invalidate),
            self.status.ready.eq(1),
        ]


class TapStatusCSRReadDUT(Module):
    def __init__(self):
        self.command = Signal()
        to_phy = PulseSynchronizer("csr", "phy")
        self.submodules.to_phy = to_phy
        self.comb += to_phy.i.eq(self.command)
        self.tree = ClockDomainsRenamer({"sys": "phy"})(RegisteredTapStatus(1))
        self.submodules.tree = self.tree
        self.bridge = CSRStatusBridge(1, "phy", "csr")
        self.submodules.bridge = self.bridge
        self.comb += [
            self.tree.source.eq(0x55),
            self.tree.ready.eq(1),
            self.tree.change.eq(to_phy.o),
            self.bridge.source.eq(self.tree.csr_valid),
            self.bridge.invalidate.eq(to_phy.o),
            self.bridge.source_invalidate.eq(self.command),
        ]

class TapStatusTest(unittest.TestCase):
    def test_csr_status_bridge_registers_level_and_invalidation(self):
        dut = CSRStatusBridge(1, "phy", "csr")
        observed = []

        def phy_driver():
            yield dut.source.eq(1)
            for _ in range(12):
                yield
            # A one-cycle invalidation event must cross despite the slower
            # CSR clock, while the returned status level is still settling.
            yield dut.invalidate.eq(1)
            yield dut.source.eq(0)
            yield
            yield dut.invalidate.eq(0)
            for _ in range(40):
                yield
            yield dut.source.eq(1)
            for _ in range(160):
                yield

        def csr_monitor():
            yield "passive"
            while True:
                yield
                observed.append((yield dut.status))

        run_simulation(dut, {"phy": phy_driver(), "csr": csr_monitor()},
            clocks={"phy": 5, "csr": 10})
        self.assertIn(1, observed)
        self.assertEqual(observed[-1], 1)
        # The invalid interval is held long enough for both CDC paths. Once
        # the returned status becomes valid again, it must not be suppressed
        # by a delayed copy of the one-shot event.
        self.assertTrue(any(observed[index:index+3] == [0, 0, 0]
            for index in range(len(observed)-2)))

    def test_immediate_csr_poll_after_command_never_reads_stale_valid(self):
        dut = TapStatusCSRReadDUT()
        observed = []

        def driver():
            for _ in range(45):
                yield
            self.assertEqual((yield dut.bridge.status), 1)
            yield dut.command.eq(1)
            yield
            yield dut.command.eq(0)
            # Model the first CSR read immediately after the write, then poll
            # through the event and status-level return crossings.
            for _ in range(8):
                yield
                observed.append((yield dut.bridge.status))
            self.assertEqual(observed[0], 0)
            self.assertEqual(observed[-1], 0)
            # A second write while status is already invalid must keep the
            # CSR latch set until that new event reaches the PHY and returns.
            yield dut.command.eq(1)
            yield
            yield dut.command.eq(0)
            for _ in range(8):
                yield
                self.assertEqual((yield dut.bridge.status), 0)

        run_simulation(dut, {"csr": driver()},
            clocks={"csr": 10, "phy": 5, "riu": 20})

    def test_source_invalidation_gates_status_immediately(self):
        dut = CSRStatusBridge(1, "phy", "csr")

        def phy_driver():
            yield dut.source.eq(1)
            for _ in range(20):
                yield

        def csr_driver():
            for _ in range(6):
                yield
            self.assertEqual((yield dut.status), 1)
            yield dut.source_invalidate.eq(1)
            yield
            yield
            self.assertEqual((yield dut.status), 0)
            yield dut.source_invalidate.eq(0)
            for _ in range(8):
                yield
                self.assertEqual((yield dut.status), 0)

        run_simulation(dut, {"phy": phy_driver(), "csr": csr_driver()},
            clocks={"phy": 5, "csr": 10})

    def test_missing_phy_event_releases_after_quiet_window(self):
        dut = CSRStatusBridge(1, "phy", "csr")

        def driver():
            yield dut.source.eq(1)
            for _ in range(6):
                yield
            self.assertEqual((yield dut.status), 1)
            # Only the immediate CSR-side event is present; no PHY event is
            # delivered to the return PulseSynchronizer.
            yield dut.source_invalidate.eq(1)
            yield
            yield
            self.assertEqual((yield dut.status), 0)
            yield dut.source_invalidate.eq(0)
            for _ in range(CSRStatusBridge.QUIET_CYCLES - 1):
                yield
                self.assertEqual((yield dut.status), 0)
            for _ in range(8):
                yield
            self.assertEqual((yield dut.status), 1)

        run_simulation(dut, {"csr": driver()}, clocks={"csr": 10, "phy": 5})

    def test_late_phy_event_restarts_full_quiet_window(self):
        dut = CSRStatusBridge(1, "phy", "csr")
        send_second = Signal()

        def phy_driver():
            yield dut.source.eq(1)
            for _ in range(12):
                yield
            yield dut.invalidate.eq(1)
            yield
            yield dut.invalidate.eq(0)
            while not (yield send_second):
                yield
            yield dut.invalidate.eq(1)
            yield
            yield dut.invalidate.eq(0)

        def csr_driver():
            while not (yield dut.transfer.o):
                yield
            for _ in range(40):
                yield
            yield send_second.eq(1)
            while not (yield dut.transfer.o):
                yield
            # The second event comes after the first quiet window has partly
            # elapsed; it must restart, rather than inherit, that deadline.
            for _ in range(32):
                yield
                self.assertEqual((yield dut.status), 0)
            for _ in range(40):
                yield
            self.assertEqual((yield dut.status), 1)

        run_simulation(dut, {"phy": phy_driver(), "csr": csr_driver()},
            clocks={"phy": 5, "csr": 10})

    def test_source_valid_low_remains_low_after_quiet_window(self):
        dut = CSRStatusBridge(1, "phy", "csr")

        def driver():
            yield dut.source.eq(0)
            yield dut.source_invalidate.eq(1)
            yield
            self.assertEqual((yield dut.status), 0)
            yield dut.source_invalidate.eq(0)
            for _ in range(CSRStatusBridge.QUIET_CYCLES + 8):
                yield
            self.assertEqual((yield dut.status), 0)

        run_simulation(dut, {"csr": driver()}, clocks={"csr": 10, "phy": 5})

    def test_phy_event_exposes_valid_source_after_quiet_window(self):
        dut = CSRStatusBridge(1, "phy", "csr")

        def phy_driver():
            yield dut.source.eq(1)
            for _ in range(12):
                yield
            yield dut.invalidate.eq(1)
            yield
            yield dut.invalidate.eq(0)

        def csr_driver():
            for _ in range(6):
                yield
            self.assertEqual((yield dut.status), 1)
            while not (yield dut.transfer.o):
                yield
            yield
            self.assertEqual((yield dut.status), 0)
            for _ in range(CSRStatusBridge.QUIET_CYCLES + 8):
                yield
            self.assertEqual((yield dut.status), 1)

        run_simulation(dut, {"phy": phy_driver(), "csr": csr_driver()},
            clocks={"phy": 5, "csr": 10})

    def test_manual_commands_align_request_and_invalidation(self):
        dut = TapCommandEventsDUT()
        observed = []

        def monitor():
            yield "passive"
            while True:
                yield
                if (yield dut.events.invalidate):
                    observed.append(((yield dut.events.requests),
                        (yield dut.status.valid)))

        def driver():
            # Wait for the tap status to become consumable before each test.
            for command_index in list(range(4)) + list(range(4)):
                for _ in range(36):
                    yield
                self.assertEqual((yield dut.status.valid), 1)

                allowed = int(command_index < 4)
                bit = command_index % 4
                previous = len(observed)
                yield dut.allowed.eq(allowed)
                yield dut.csr_commands.eq(1 << bit)
                yield
                yield dut.csr_commands.eq(0)

                for _ in range(12):
                    yield
                    if len(observed) > previous:
                        break
                self.assertEqual(len(observed), previous + 1)
                expected_requests = (1 << bit) if allowed else 0
                self.assertEqual(observed[-1], (expected_requests, 0))

            # Confirm each CSR pulse produced exactly one fast-domain event.
            for _ in range(8):
                yield
            self.assertEqual(len(observed), 8)

        run_simulation(dut, {"csr": driver(), "sys": monitor()},
            clocks={"csr": 10, "sys": 5, "riu": 20})

    def test_small_and_partial_groups(self):
        for entries in (1, 2, 7, 8, 9, 45):
            with self.subTest(entries=entries):
                dut = RegisteredTapStatus(entries)
                def driver():
                    yield dut.source.eq(sum((i+11) << (9*i) for i in range(entries)))
                    yield dut.ready.eq(1)
                    for index in range(entries):
                        yield dut.select.eq(index)
                        yield dut.change.eq(1)
                        yield
                        self.assertEqual((yield dut.valid), 0)
                        yield dut.change.eq(0)
                        for _ in range(36):
                            yield
                        self.assertEqual((yield dut.valid), 1)
                        self.assertEqual((yield dut.value), index+11)
                run_simulation(dut, driver(), clocks={'sys': 10, 'riu': 20})

    def test_invalid_entry_count(self):
        for entries in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                RegisteredTapStatus(entries)

    def test_all_selections_settle_and_reset(self):
        dut = RegisteredTapStatus(105)
        def driver():
            values = [(i*3+1)%512 for i in range(105)]
            yield dut.source.eq(sum(v<<(9*i) for i, v in enumerate(values)))
            yield dut.ready.eq(1)
            for index in range(105):
                yield dut.select.eq(index)
                yield dut.change.eq(1)
                yield
                self.assertEqual((yield dut.valid), 0)
                yield dut.change.eq(0)
                for cycle in range(36):
                    yield
                    if (yield dut.valid):
                        self.assertEqual((yield dut.value), values[index])
                self.assertEqual((yield dut.valid), 1)
            # Count changes after an operation traverse RIU capture and tree.
            yield dut.change.eq(1)
            yield
            yield dut.change.eq(0)
            for _ in range(12):
                yield
            values[-1] = 377
            yield dut.source.eq(sum(v<<(9*i) for i, v in enumerate(values)))
            for _ in range(25):
                yield
            self.assertEqual((yield dut.valid), 1)
            self.assertEqual((yield dut.value), 377)
            yield dut.ready.eq(0)
            yield
            self.assertEqual((yield dut.valid), 0)
        run_simulation(dut, driver(), clocks={'sys':10, 'riu':20})
    def test_group_changes_and_out_of_range_stay_invalid_until_settled(self):
        dut = RegisteredTapStatus(105)
        values = [(i*7+3) % 512 for i in range(105)]
        def driver():
            yield dut.source.eq(sum(value << (9*i) for i, value in enumerate(values)))
            yield dut.ready.eq(1)
            for selected in (7, 8, 63, 64, 104, 105, 127, 0):
                yield dut.select.eq(selected)
                yield dut.change.eq(1)
                yield
                self.assertEqual((yield dut.valid), 0)
                yield dut.change.eq(0)
                for _ in range(36):
                    yield
                    if (yield dut.valid):
                        self.assertEqual((yield dut.value), values[selected] if selected < 105 else 0)
                self.assertEqual((yield dut.valid), 1)
        run_simulation(dut, driver(), clocks={"sys": 10, "riu": 20})

if __name__=='__main__':
    unittest.main()
