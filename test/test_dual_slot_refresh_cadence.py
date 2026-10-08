# SPDX-License-Identifier: BSD-2-Clause
"""Measure AES-KU40 DDR4-2400 1:8 refresh commands at the controller DFI."""

import unittest

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.modules import EDY4016A


SYS_HZ = 150e6
NOMINAL_TREFI_NS = 64e6 / 8192


def make_controller():
    module = EDY4016A(SYS_HZ, "1:8", speedgrade="2400")
    phy = PhySettings(
        phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
        dfi_databits=64, nphases=8, nranks=1,
        rdphase=2, wrphase=3, cl=17, cwl=12, cmd_latency=5,
        read_latency=9, write_latency=1)
    settings = ControllerSettings(
        with_bank_group_interleaving=True,
        with_dual_slot=True,
        with_auto_precharge=False,
        with_refresh=True,
        refresh_postponing=1,
        with_registered_row_hit=True,
        with_registered_refresh_timers=True,
        with_registered_refresh_request=True,
        with_registered_timing_valid=True,
        with_activate_eligibility=True,
        read_time=32,
        write_time=16)
    controller = LiteDRAMController(
        phy, module.geom_settings, module.timing_settings, SYS_HZ,
        controller_settings=settings)
    return controller, module


def run_case(sustained_traffic):
    controller, module = make_controller()
    interface = controller.interface
    top = Module()
    top.clock_domains.cd_sys = ClockDomain("sys")
    top.submodules.controller = controller

    # Three real refresh intervals give two measured gaps. Include enough
    # time after the third REF to observe tRFC completion.
    horizon = module.timing_settings.tREFI * 3 + module.timing_settings.tRFC + 32
    events = {"prea": [], "ref": [], "cas": [], "commands": [], "errors": []}

    def drive():
        read_ready = (1 << 8) - 1 if sustained_traffic else 0
        for bank in range(8):
            port = getattr(interface, f"bank{bank}")
            yield port.addr.eq(0)
            yield port.we.eq(0)
            yield port.valid.eq(int(sustained_traffic))
        yield interface.bank_read_ready.eq(read_ready)
        yield interface.bank_write_ready.eq((1 << 8) - 1)
        for _ in range(horizon):
            yield

    def monitor():
        for cycle in range(horizon):
            dfi_events = []
            for phase_index, phase in enumerate(controller.dfi.phases):
                cs_n = (yield phase.cs_n)
                ras_n = (yield phase.ras_n)
                cas_n = (yield phase.cas_n)
                we_n = (yield phase.we_n)
                address = (yield phase.address)
                if cs_n:
                    continue
                if not ras_n and cas_n and not we_n and (address & (1 << 10)):
                    dfi_events.append(("PREA", phase_index, address))
                elif not ras_n and not cas_n and we_n:
                    dfi_events.append(("REF", phase_index, address))
                elif not ras_n and cas_n and not we_n:
                    dfi_events.append(("PRE", phase_index, address))
                elif not ras_n and cas_n and we_n:
                    dfi_events.append(("ACT", phase_index, address))
                elif ras_n and not cas_n:
                    dfi_events.append(("CAS", phase_index, we_n))

            if len([event for event in dfi_events if event[0] in ("PREA", "REF")]) > 1:
                events["errors"].append((cycle, "multiple maintenance commands", dfi_events))
            for kind, phase, value in dfi_events:
                events["commands"].append((cycle, kind, phase, value))
                if kind == "PREA":
                    events["prea"].append((cycle, phase, value))
                elif kind == "REF":
                    events["ref"].append((cycle, phase, value))
                else:
                    events["cas"].append((cycle, phase, value))
            yield

    fragment = top.get_fragment()
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, {"sys": [drive(), monitor()]})
    events["module_timing"] = module.timing_settings
    return events


class DualSlotRefreshCadenceTest(unittest.TestCase):
    def check_case(self, sustained_traffic):
        events = run_case(sustained_traffic)
        timing = events["module_timing"]
        self.assertEqual(timing.tREFI, 1172)
        self.assertEqual(timing.tRP, 3)
        self.assertEqual(timing.tRFC, 40)
        self.assertEqual(events["errors"], [])
        self.assertGreaterEqual(len(events["ref"]), 3, events["ref"])
        self.assertEqual([phase for _, phase, _ in events["ref"]],
                         [0] * len(events["ref"]))
        self.assertEqual([phase for _, phase, _ in events["prea"]],
                         [0] * len(events["prea"]))

        # Every REF must be preceded by its PREA after at least tRP cycles;
        # the next REF must not violate tRFC recovery.
        prea_gaps = []
        for ref_cycle, _, _ in events["ref"]:
            prior_precharges = [cycle for cycle, _, _ in events["prea"]
                                if cycle < ref_cycle]
            self.assertTrue(prior_precharges, events)
            gap = ref_cycle - prior_precharges[-1]
            prea_gaps.append(gap)
            self.assertGreaterEqual(gap, timing.tRP,
                                    (ref_cycle, prior_precharges[-1]))
        post_ref_command_gaps = []
        for ref_cycle, _, _ in events["ref"]:
            following = [cycle for cycle, _, _, _ in events["commands"]
                         if cycle > ref_cycle]
            if following:
                gap = min(following) - ref_cycle
                post_ref_command_gaps.append(gap)
                self.assertGreaterEqual(gap, timing.tRFC, (ref_cycle, gap))
        for (ref0, _, _), (ref1, _, _) in zip(events["ref"], events["ref"][1:]):
            self.assertGreaterEqual(ref1 - ref0, timing.tRFC, (ref0, ref1))

        gaps = [b[0] - a[0] for a, b in zip(events["ref"], events["ref"][1:])]
        self.assertTrue(all(gap == timing.tREFI for gap in gaps),
                        ("refresh cadence", gaps))
        maximum_ns = max(gaps) * 1e9 / SYS_HZ
        nominal_cycles = NOMINAL_TREFI_NS * SYS_HZ / 1e9
        print("refresh cadence:", {
            "traffic": "sustained alternating banks" if sustained_traffic else "idle",
            "ref_cycles": [event[0] for event in events["ref"]],
            "gaps_cycles": gaps,
            "prea_to_ref_cycles": prea_gaps,
            "ref_to_first_following_command_cycles": post_ref_command_gaps,
            "max_gap_ns": maximum_ns,
            "nominal_tREFI_ns": NOMINAL_TREFI_NS,
            "nominal_cycles_exact": nominal_cycles,
            "max_gap_excess_ns": maximum_ns - NOMINAL_TREFI_NS,
            "cas_commands": len(events["cas"]),
        })
        self.assertGreater(maximum_ns, NOMINAL_TREFI_NS,
                           "This test records the current ceil-rounded cadence")
        if sustained_traffic:
            self.assertGreater(len(events["cas"]), 1000,
                               "traffic case must keep alternating bank groups active")

    def test_idle_refresh_cadence_and_boundaries(self):
        self.check_case(sustained_traffic=False)

    def test_sustained_alternating_bank_refresh_cadence_and_boundaries(self):
        self.check_case(sustained_traffic=True)


if __name__ == "__main__":
    unittest.main()
