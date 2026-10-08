# SPDX-License-Identifier: BSD-2-Clause
"""Refresh drains pending dual-slot bank commands and resumes queued traffic."""

import unittest

from migen import *
from litex.soc.interconnect import stream
from litex.gen.sim import run_simulation

from litedram.common import PhySettings, cmd_request_rw_layout
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.refresher import Refresher
from litedram.modules import EDY4016A


class ManualRefresher(Module):
    """A deterministic PREA/tRP/REF source for controller-level tests."""
    def __init__(self, settings, clk_freq, zqcs_freq=1.0, postponing=1):
        self.start = Signal()
        self.cmd = stream.Endpoint(cmd_request_rw_layout(
            settings.geom.addressbits,
            settings.geom.bankbits + log2_int(settings.phy.nranks)))
        active = Signal()
        stage = Signal(3)  # PREA, tRP wait, REF, tRFC wait, completion
        wait = Signal(3)
        self.comb += [self.cmd.valid.eq(active & (stage != 4)),
                      self.cmd.last.eq(active & (stage == 4)),
                      self.cmd.a.eq(1 << 10), self.cmd.ba.eq(0),
                      self.cmd.ras.eq(0), self.cmd.cas.eq(0), self.cmd.we.eq(0),
                      self.cmd.is_cmd.eq(0), self.cmd.is_read.eq(0),
                      self.cmd.is_write.eq(0)]
        self.comb += If(stage == 0,
            self.cmd.ras.eq(1), self.cmd.we.eq(1), self.cmd.is_cmd.eq(1)
        ).Elif(stage == 2,
            self.cmd.ras.eq(1), self.cmd.cas.eq(1), self.cmd.is_cmd.eq(1)
        )
        self.sync += If(self.start,
            active.eq(1), stage.eq(0), wait.eq(0)
        ).Elif(active & (stage == 0) & self.cmd.ready,
            stage.eq(1), wait.eq(2)
        ).Elif(active & (stage == 1),
            If(wait == 0, stage.eq(2)).Else(wait.eq(wait - 1))
        ).Elif(active & (stage == 2) & self.cmd.ready,
            stage.eq(3), wait.eq(4)
        ).Elif(active & (stage == 3),
            If(wait == 0, stage.eq(4)).Else(wait.eq(wait - 1))
        ).Elif(active & (stage == 4) & self.cmd.ready,
            active.eq(0)
        )


def make_controller():
    frequency = 150e6
    module = EDY4016A(frequency, "1:8", speedgrade="2400")
    module.timing_settings.tCCD = 1
    module.timing_settings.tREFI = 120
    # Hold ACT and PRE candidates behind realistic, deliberately long timers
    # so the test can request refresh while each state has a pending command.
    module.timing_settings.tRRD = 12
    module.timing_settings.tFAW = 24
    module.timing_settings.tRAS = max(module.timing_settings.tRAS, 40)
    module.timing_settings.tRCD = max(module.timing_settings.tRCD, 2)
    phy = PhySettings(
        phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
        dfi_databits=64, nphases=8, rdphase=2, wrphase=3, cl=17,
        cwl=12, cmd_latency=5, read_latency=9, write_latency=1, nranks=1)
    settings = ControllerSettings(
        with_bank_group_interleaving=True, with_dual_slot=True,
        with_auto_precharge=False, with_refresh=True,
        refresh_cls=ManualRefresher, refresh_postponing=1,
        with_registered_row_hit=True, with_registered_refresh_request=True,
        with_registered_timing_valid=True, with_activate_eligibility=True,
        read_time=32, write_time=16)
    controller = LiteDRAMController(
        phy, module.geom_settings, module.timing_settings, frequency,
        controller_settings=settings)
    return controller


def make_periodic_controller():
    """Use the real timer/sequencer with a short, deterministic tREFI."""
    frequency = 150e6
    module = EDY4016A(frequency, "1:8", speedgrade="2400")
    module.timing_settings.tCCD = 1
    module.timing_settings.tREFI = 120
    phy = PhySettings(
        phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
        dfi_databits=64, nphases=8, rdphase=2, wrphase=3, cl=17,
        cwl=12, cmd_latency=5, read_latency=9, write_latency=1, nranks=1)
    settings = ControllerSettings(
        with_bank_group_interleaving=True, with_dual_slot=True,
        with_auto_precharge=False, with_refresh=True, refresh_cls=Refresher,
        refresh_postponing=1, with_registered_row_hit=True,
        with_registered_refresh_timers=True,
        with_registered_refresh_request=True,
        with_registered_timing_valid=True, with_activate_eligibility=True,
        read_time=32, write_time=16)
    return LiteDRAMController(
        phy, module.geom_settings, module.timing_settings, frequency,
        controller_settings=settings)


class DualSlotRefreshTest(unittest.TestCase):
    def test_periodic_refresh_under_continuous_cas_and_backpressure(self):
        controller = make_periodic_controller()
        top = Module()
        top.submodules.controller = controller
        interface = controller.interface
        mux = controller.multiplexer
        refresher = controller.refresher
        bank_machines = [m for _, m in controller._submodules
                         if hasattr(m, "activate_ready")]
        self.assertEqual(len(bank_machines), 8)

        horizon = 900
        events = {"refresh": [], "cas": 0, "cas_during_refresh": [],
                  "request_cycles": [], "grant_wait": 0, "max_grant_wait": 0}

        def monitor():
            for cycle in range(horizon):
                pending = (yield mux.arbiter.refresh_req)
                if (yield refresher.cmd.valid):
                    events["request_cycles"].append(cycle)
                for slot, command in enumerate(mux.arbiter.cas_cmd):
                    if (yield command.valid) and (yield command.ready):
                        events["cas"] += 1
                        if pending:
                            events["cas_during_refresh"].append((cycle, slot))
                grants = 0
                for bank in bank_machines:
                    grants += (yield bank.refresh_gnt)
                if pending:
                    if grants == len(bank_machines):
                        events["max_grant_wait"] = max(
                            events["max_grant_wait"], events["grant_wait"])
                        events["grant_wait"] = 0
                    else:
                        events["grant_wait"] += 1
                for phase_index, phase in enumerate(controller.dfi.phases):
                    if ((yield phase.cs_n) == 0 and (yield phase.ras_n) == 0 and
                            (yield phase.cas_n) == 0 and (yield phase.we_n) == 1):
                        events["refresh"].append(cycle)
                yield

        def driver():
            ports = [getattr(interface, "bank" + str(i)) for i in range(8)]
            for port in ports:
                yield port.addr.eq(0)
                yield port.we.eq(0)
                yield port.valid.eq(1)
            # Alternate backpressure by bank group while keeping requests
            # continuously queued at every bank.
            for cycle in range(horizon):
                ready = 0xff
                if cycle % 24 < 6:
                    ready &= 0xf0
                elif cycle % 24 < 12:
                    ready &= 0x0f
                yield interface.bank_read_ready.eq(ready)
                yield interface.bank_write_ready.eq(0xff)
                yield

        fragment = top.get_fragment()
        for special in fragment.specials:
            if isinstance(special, Memory):
                for port in special.ports:
                    if port.dat_r is None:
                        port.dat_r = Signal(special.width)
        run_simulation(fragment, {"sys": [driver(), monitor()]})

        refreshes = events["refresh"]
        self.assertGreaterEqual(len(refreshes), 5, refreshes)
        self.assertGreater(events["cas"], 100, events)
        self.assertEqual(events["cas_during_refresh"], [], events)
        self.assertLess(events["max_grant_wait"], 120, events)
        gaps = [b - a for a, b in zip(refreshes, refreshes[1:])]
        # A queued command/timing drain may delay the periodic request from
        # reaching the DFI, but must not postpone it by another refresh period.
        self.assertTrue(all(120 <= gap < 240 for gap in gaps), (refreshes, gaps))

    def run_case(self, pending_state):
        controller = make_controller()
        top = Module()
        top.submodules.controller = controller
        interface = controller.interface
        bank_machines = [m for _, m in controller._submodules
                         if hasattr(m, "activate_ready")]
        refresher = controller.refresher
        wait_state = Signal()
        wait_command = Signal()
        activation_allowed = Signal()
        mux_read = Signal()
        top.comb += mux_read.eq(controller.multiplexer.fsm.ongoing("READ"))
        if pending_state == "ACTIVATE":
            top.comb += wait_state.eq(bank_machines[4].fsm.ongoing("ACTIVATE"))
            wait_bank = bank_machines[4]
            top.comb += [wait_command.eq(wait_bank.cmd.valid),
                         activation_allowed.eq(controller.multiplexer.trrd.ready &
                                               controller.multiplexer.tfaw.ready)]
        else:
            top.comb += wait_state.eq(bank_machines[0].fsm.ongoing("PRECHARGE"))
            wait_bank = bank_machines[0]
            top.comb += [wait_command.eq(wait_bank.cmd.valid),
                         activation_allowed.eq(1)]
        cycle_counter = Signal(16)
        top.sync += cycle_counter.eq(cycle_counter + 1)
        events = {"commands": [], "refresh_start": None, "refresh_done": None,
                  "trigger_seen": False}
        horizon = 300
        bank0 = interface.bank0
        bank4 = interface.bank4

        def address(row, col=0):
            return (row << 7) | col

        def command_monitor():
            for cycle in range(horizon):
                for phase_index, phase in enumerate(controller.dfi.phases):
                    cs_n = (yield phase.cs_n)
                    ras_n = (yield phase.ras_n)
                    cas_n = (yield phase.cas_n)
                    we_n = (yield phase.we_n)
                    bank = (yield phase.bank)
                    if cs_n:
                        continue
                    if not ras_n and cas_n and we_n:
                        kind = "ACT"
                    elif not ras_n and cas_n and not we_n:
                        kind = "PRE"
                    elif not ras_n and not cas_n and we_n:
                        kind = "REF"
                    elif ras_n and not cas_n:
                        kind = "CAS"
                    else:
                        continue
                    events["commands"].append((cycle, kind, phase_index, bank))
                    if kind == "REF":
                        events["refresh_done"] = cycle
                if (yield refresher.start):
                    events["trigger_seen"] = True
                    if events["refresh_start"] is None:
                        events["refresh_start"] = cycle
                yield

        def driver():
            yield interface.bank_read_ready.eq((1 << 8) - 1)
            yield interface.bank_write_ready.eq((1 << 8) - 1)
            yield bank0.valid.eq(0)
            yield bank4.valid.eq(0)
            yield refresher.start.eq(0)
            yield

            # Open bank 0's row first. This ACT arms the global tRRD window.
            yield bank0.addr.eq(address(1))
            yield bank0.we.eq(0)
            yield bank0.valid.eq(1)
            while True:
                if (yield bank0.ready):
                    yield
                    break
                yield
            yield bank0.valid.eq(0)

            if pending_state == "ACTIVATE":
                # Bank 4 waits in ACTIVATE while the first ACT's tRRD/tFAW
                # admission is still closed.
                yield bank4.addr.eq(address(2))
                yield bank4.we.eq(0)
                yield bank4.valid.eq(1)
                while True:
                    if (yield bank4.ready):
                        yield
                        break
                    yield
                yield bank4.valid.eq(0)
                for _ in range(80):
                    if ((yield wait_state) and (yield wait_command) and
                            not (yield activation_allowed)):
                        break
                    yield
                else:
                    self.fail("bank 4 did not reach timing-blocked ACTIVATE; "
                              f"commands={events['commands']}")
            else:
                # Queue a different row in the same bank. The BankMachine
                # holds PRECHARGE until tRAS expires.
                yield bank0.addr.eq(address(3))
                yield bank0.we.eq(0)
                yield bank0.valid.eq(1)
                while True:
                    if (yield bank0.ready):
                        yield
                        break
                    yield
                yield bank0.valid.eq(0)
                for _ in range(80):
                    if ((yield wait_state) and not (yield wait_bank.trascon.ready)):
                        break
                    yield
                else:
                    self.fail("bank 0 did not reach tRAS-blocked PRECHARGE; "
                              f"commands={events['commands']}")

            yield refresher.start.eq(1)
            events["refresh_start"] = (yield cycle_counter)
            yield
            yield refresher.start.eq(0)

            # Wait for one physical REF command and the fake refresher to
            # release its persistent request.
            saw_refresh_request = False
            for _ in range(120):
                saw_refresh_request |= bool((yield refresher.cmd.valid))
                if (saw_refresh_request and (yield mux_read) and
                        not (yield refresher.cmd.valid)):
                    break
                yield
            else:
                self.fail("refresh did not complete")

            # The read queued behind the state that drained for refresh must
            # eventually resume and emit a CAS.
            for _ in range(100):
                refresh_cycles = [cycle for cycle, kind, _p, _b in events["commands"]
                                  if kind == "REF"]
                if refresh_cycles and any(
                        kind == "CAS" and cycle > refresh_cycles[0]
                        for cycle, kind, _p, _b in events["commands"]):
                    break
                yield
            else:
                self.fail("queued CAS traffic did not resume after refresh; "
                          f"commands={events['commands']}")

        fragment = top.get_fragment()
        for special in fragment.specials:
            if isinstance(special, Memory):
                for port in special.ports:
                    if port.dat_r is None:
                        port.dat_r = Signal(special.width)
        run_simulation(fragment, {"sys": [driver(), command_monitor()]})

        refreshes = [event for event in events["commands"] if event[1] == "REF"]
        self.assertEqual(len(refreshes), 1, events["commands"])
        ref_cycle = refreshes[0][0]
        self.assertTrue(events["trigger_seen"])
        pre_ref_cas = [event for event in events["commands"]
                       if event[1] == "CAS" and event[0] > events["refresh_start"]
                       and event[0] < ref_cycle]
        self.assertEqual(pre_ref_cas, [], events["commands"])
        self.assertTrue(any(kind == "CAS" and cycle > ref_cycle
                            for cycle, kind, _phase, _bank in events["commands"]),
                        events["commands"])

    def test_refresh_drains_timing_blocked_activate(self):
        self.run_case("ACTIVATE")

    def test_refresh_drains_timing_blocked_precharge(self):
        self.run_case("PRECHARGE")


if __name__ == "__main__":
    unittest.main()
