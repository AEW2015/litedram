#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest

from migen import *

from litex.soc.interconnect import stream
from litex.gen.sim import run_simulation

from litedram.common import GeomSettings, PhySettings, TimingSettings, cmd_request_rw_layout
from litedram.core.controller import ControllerSettings, LiteDRAMController


class ManualRefresher(Module):
    """A controllable refresh request source for controller timing tests."""

    def __init__(self, settings, clk_freq, zqcs_freq, postponing):
        addressbits = settings.geom.addressbits
        bankbits = settings.geom.bankbits + log2_int(settings.phy.nranks)
        self.cmd = stream.Endpoint(cmd_request_rw_layout(addressbits, bankbits))


class TestActivateEligibilityController(unittest.TestCase):
    def test_t_rc_blocked_activate_refresh_and_following_request(self):
        phy_settings = PhySettings(
            phytype="test",
            memtype="DDR3",
            databits=16,
            dfi_databits=32,
            nphases=2,
            rdphase=0,
            wrphase=1,
            cl=3,
            read_latency=5,
            write_latency=2,
            cwl=2,
            nranks=1,
        )
        geom_settings = GeomSettings(bankbits=3, rowbits=8, colbits=10)
        t_rc = 24
        timing_settings = TimingSettings(
            tRP=1,
            tRCD=2,
            tWR=1,
            tWTR=1,
            tREFI=1000,
            tRFC=2,
            tFAW=None,
            tCCD=1,
            tRRD=None,
            tRC=t_rc,
            tRAS=1,
            tZQCS=None,
        )
        controller = LiteDRAMController(
            phy_settings,
            geom_settings,
            timing_settings,
            clk_freq=100e6,
            controller_settings=ControllerSettings(
                with_activate_eligibility=True,
                with_auto_precharge=False,
                refresh_cls=ManualRefresher,
            ),
        )

        events = {"cycle": 0, "acts": [], "precharges": 0, "refreshes": 0}
        bank0_acts = []
        first_activate = {"cycle": None}
        split = geom_settings.colbits - log2_int(8)

        @passive
        def monitor():
            while True:
                for phase in controller.dfi.phases:
                    cas_n = (yield phase.cas_n)
                    ras_n = (yield phase.ras_n)
                    we_n = (yield phase.we_n)
                    bank = (yield phase.bank)
                    if (cas_n, ras_n, we_n) == (1, 0, 1):
                        events["acts"].append((events["cycle"], bank))
                        if bank == 0:
                            bank0_acts.append(events["cycle"])
                            if first_activate["cycle"] is None:
                                first_activate["cycle"] = events["cycle"]
                            elif len(bank0_acts) == 2:
                                self.assertGreaterEqual(
                                    events["cycle"] - first_activate["cycle"], t_rc,
                                    "second ACT was issued before tRC elapsed",
                                )
                    elif (cas_n, ras_n, we_n) == (1, 0, 0):
                        events["precharges"] += 1
                    elif (cas_n, ras_n, we_n) == (0, 0, 1):
                        events["refreshes"] += 1
                events["cycle"] += 1
                yield

        def submit_read(port, row):
            yield port.addr.eq(row << split)
            yield port.we.eq(0)
            yield port.valid.eq(1)
            for _ in range(20):
                yield
                if (yield port.ready):
                    break
            self.assertEqual((yield port.ready), 1, "request was not accepted")
            yield port.valid.eq(0)

        def main():
            refresher = controller.refresher
            yield refresher.cmd.valid.eq(0)
            yield refresher.cmd.cas.eq(1)
            yield refresher.cmd.ras.eq(1)
            yield refresher.cmd.we.eq(0)
            yield refresher.cmd.is_cmd.eq(1)
            yield refresher.cmd.last.eq(0)

            bank0 = controller.interface.bank0
            bank1 = controller.interface.bank1
            yield from submit_read(bank0, row=0)
            for _ in range(100):
                if (yield bank0.rdata_valid):
                    break
                yield
            self.assertEqual((yield bank0.rdata_valid), 1, "first read did not issue")

            # A row miss forces PRECHARGE followed by an ACT that must wait
            # for the first ACT's tRC timer.
            yield from submit_read(bank0, row=1)
            for _ in range(100):
                if events["precharges"]:
                    break
                yield
            self.assertGreater(events["precharges"], 0, "row miss did not precharge")

            # Request refresh while the second ACT is still tRC-ineligible.
            self.assertEqual(len(bank0_acts), 1)
            self.assertLess(
                events["cycle"] - first_activate["cycle"], t_rc,
                "precharge completed only after the tRC wait ended",
            )
            yield refresher.cmd.valid.eq(1)
            for _ in range(200):
                if events["refreshes"]:
                    break
                yield
            self.assertGreater(events["refreshes"], 0, "refresh command did not issue")
            self.assertEqual(len(bank0_acts), 2)

            # Let the refresh sequencer finish and release the controller.
            yield refresher.cmd.last.eq(1)
            yield refresher.cmd.valid.eq(0)
            yield
            yield refresher.cmd.last.eq(0)
            for _ in range(20):
                if controller.multiplexer.fsm.decoding[(yield controller.multiplexer.fsm.state)] == "READ":
                    break
                yield
            self.assertEqual(
                controller.multiplexer.fsm.decoding[(yield controller.multiplexer.fsm.state)],
                "READ",
                "controller did not leave refresh",
            )

            yield from submit_read(bank1, row=0)
            for _ in range(100):
                if any(bank == 1 for _, bank in events["acts"]):
                    break
                yield
            bank_machines = [module for _, module in controller._submodules
                if hasattr(module, "activate_ready")]
            bank1_fsm = bank_machines[1].fsm
            self.assertTrue(
                any(bank == 1 for _, bank in events["acts"]),
                "request after refresh did not progress: {}, mux={}, bm1={}, "
                "valid={}, ready={}, refresh_req={}".format(
                    events,
                    controller.multiplexer.fsm.decoding[(yield controller.multiplexer.fsm.state)],
                    bank1_fsm.decoding[(yield bank1_fsm.state)],
                    (yield bank1.valid),
                    (yield bank1.ready),
                    (yield bank_machines[1].refresh_req),
                ),
            )

        # The pinned Migen simulator expects a read-data wire on FIFO memory
        # ports even though these synchronous FIFOs only use writes.
        fragment = controller.get_fragment()
        for special in fragment.specials:
            if isinstance(special, Memory):
                for port in special.ports:
                    if port.dat_r is None:
                        port.dat_r = Signal(special.width)
        run_simulation(fragment, [main(), monitor()])


if __name__ == "__main__":
    unittest.main()
