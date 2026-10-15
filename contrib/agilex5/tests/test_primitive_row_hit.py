"""Cycle equivalence for BankMachine's optional registered row-hit path."""
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for source in (ROOT / "third_party" / "litedram",
               ROOT / "third_party" / "litex",
               ROOT / "third_party" / "migen",
               ROOT):
    sys.path.insert(0, str(source))

from migen import *
from migen.sim import run_simulation
from migen.fhdl.decorators import ResetInserter
from litedram.common import GeomSettings, PhySettings, TimingSettings
from litedram.core.bankmachine import BankMachine
from litedram.core.controller import ControllerSettings


class PrimitiveRowHitTests(unittest.TestCase):
    def _settings(self, registered):
        settings = ControllerSettings(
            cmd_buffer_depth=4,
            cmd_buffer_buffered=True,
            with_auto_precharge=False,
            with_refresh=True,
        )
        settings.registered_row_hit = registered
        settings.geom = GeomSettings(bankbits=1, rowbits=13, colbits=10)
        settings.timing = TimingSettings(
            tRP=2, tRCD=2, tWR=2, tWTR=2, tREFI=16, tRFC=4,
            tFAW=4, tCCD=2, tRRD=2, tRC=6, tRAS=4, tZQCS=4)
        settings.phy = PhySettings(
            phytype="sim", memtype="DDR3", databits=16, dfi_databits=16,
            nphases=2, rdphase=0, wrphase=0, cl=5, read_latency=5,
            write_latency=5, cwl=5)
        return settings

    def test_registered_hit_matches_combinational_bankmachine_cycle_by_cycle(self):
        reference = BankMachine(0, address_width=23, address_align=3, nranks=1,
                                settings=self._settings(False))
        registered = BankMachine(0, address_width=23, address_align=3, nranks=1,
                                 settings=self._settings(True))
        dut = Module()
        dut.submodules.reference = reference
        dut.submodules.registered = registered

        commands = [
            (0, 0), (0, 0x100), (1, 0x200), (0, 0x100),
            (1, 0x100), (0, 0x100), (0, 0x400), (1, 0x400),
            (0, 0x200), (1, 0x700), (0, 0x700), (0, 0x700),
        ]
        trace = []

        def bench():
            sent = 0
            valid = False
            current = None
            for cycle in range(220):
                # Present each command until the input FIFO accepts it.
                if not valid and sent < len(commands):
                    current = commands[sent]
                    valid = True
                for bm in (reference, registered):
                    yield bm.req.valid.eq(valid)
                    if current is not None:
                        we, addr = current
                        yield bm.req.we.eq(we)
                        yield bm.req.addr.eq(addr)
                    # Stall command output often, including around row changes.
                    yield bm.cmd.ready.eq(int(cycle % 7 not in (2, 3)))
                    # Refresh while commands are queued and while idle.
                    yield bm.refresh_req.eq(int(cycle in (25, 26, 90, 91)))
                yield
                # Compare visible behavior and internal transaction handshakes.
                names = ("ready", "lock", "wdata_ready", "rdata_valid")
                outputs = ("valid", "a", "ba", "cas", "ras", "we",
                           "is_read", "is_write", "is_cmd")
                got = []
                for bm in (reference, registered):
                    sample = {}
                    for name in names:
                        sample[name] = (yield getattr(bm.req, name))
                    sample["refresh_gnt"] = (yield bm.refresh_gnt)
                    command_values = []
                    for name in outputs:
                        command_values.append((yield getattr(bm.cmd, name)))
                    sample["cmd"] = tuple(command_values)
                    got.append(sample)
                self.assertEqual(got[0], got[1], f"cycle {cycle}")
                trace.append(got[0])
                if valid and got[0]["ready"]:
                    sent += 1
                    valid = False
                    current = None
            self.assertEqual(sent, len(commands), "all input requests should be accepted")
            self.assertTrue(any(sample["cmd"][0] for sample in trace), "commands were emitted")
            self.assertTrue(any(sample["refresh_gnt"] for sample in trace), "refresh was granted")
            self.assertTrue(any(sample["cmd"][6] for sample in trace), "writes were emitted")
            self.assertTrue(any(sample["cmd"][7] for sample in trace), "reads were emitted")

        run_simulation(dut, bench())

    def test_ddr4_controller_settings_match_through_cold_and_midstream_reset(self):
        # Values from build/public-litedram-1333/controller.json.
        def controller_settings(registered):
            settings = ControllerSettings(
                cmd_buffer_depth=1,
                cmd_buffer_buffered=True,
                with_auto_precharge=False,
                with_refresh=True,
            )
            settings.registered_row_hit = registered
            settings.geom = GeomSettings(bankbits=4, rowbits=17, colbits=10)
            settings.timing = TimingSettings(
                tRP=4, tRCD=4, tWR=4, tWTR=3, tREFI=1300, tRFC=60,
                tFAW=5, tCCD=1, tRRD=2, tRC=11, tRAS=7, tZQCS=16)
            settings.phy = PhySettings(
                phytype="sim", memtype="DDR4", databits=32, dfi_databits=32,
                nphases=8, rdphase=3, wrphase=2, cl=20, read_latency=14,
                write_latency=1, cwl=14)
            return settings

        reference = ResetInserter()(BankMachine(
            0, address_width=24, address_align=3, nranks=1,
            settings=controller_settings(False)))
        registered = ResetInserter()(BankMachine(
            0, address_width=24, address_align=3, nranks=1,
            settings=controller_settings(True)))
        dut = Module()
        dut.submodules.reference = reference
        dut.submodules.registered = registered

        rng = random.Random(0x51A7)
        requests = []
        row = 0
        for i in range(72):
            if i == 0 or rng.random() >= 0.68:
                row = rng.randrange(8)
            column = rng.randrange(1 << 7)
            requests.append((rng.randrange(2), (row << 7) | column))
        resets = {0, 1, 155, 156}
        trace = []

        def bench():
            next_request = 0
            pending = None
            sent = 0
            for cycle in range(440):
                reset = int(cycle in resets)
                if not reset and pending is None and next_request < len(requests):
                    pending = requests[next_request]
                ready = int(rng.random() >= 0.28)
                refresh = int(any(start <= cycle < start + 18 for start in (35, 125, 245, 335)))
                for bm in (reference, registered):
                    yield bm.reset.eq(reset)
                    yield bm.req.valid.eq(int(pending is not None and not reset))
                    if pending is not None:
                        yield bm.req.we.eq(pending[0])
                        yield bm.req.addr.eq(pending[1])
                    yield bm.cmd.ready.eq(ready)
                    yield bm.refresh_req.eq(refresh)
                yield

                fields = ("ready", "lock", "wdata_ready", "rdata_valid")
                cmd_fields = ("valid", "a", "ba", "cas", "ras", "we",
                              "is_read", "is_write", "is_cmd")
                pair = []
                for bm in (reference, registered):
                    sample = {}
                    for name in fields:
                        sample[name] = (yield getattr(bm.req, name))
                    sample["refresh_gnt"] = (yield bm.refresh_gnt)
                    cmd = []
                    for name in cmd_fields:
                        cmd.append((yield getattr(bm.cmd, name)))
                    sample["cmd"] = tuple(cmd)
                    # Compare timing-controller state at every edge as well as
                    # the emitted command stream.
                    timing_ready = []
                    for name in ("twtpcon", "trccon", "trascon"):
                        timing_ready.append((yield getattr(getattr(bm, name), "ready")))
                    sample["timing_ready"] = tuple(timing_ready)
                    pair.append(sample)
                self.assertEqual(pair[0], pair[1], f"cycle {cycle}")
                trace.append(pair[0])

                if not reset and pending is not None and pair[0]["ready"]:
                    next_request += 1
                    sent += 1
                    pending = None
            self.assertGreater(sent, 50, "sufficient requests should be accepted")
            commands_seen = [sample["cmd"] for sample in trace]
            self.assertTrue(any(c[0] and c[6] for c in commands_seen), "reads emitted")
            self.assertTrue(any(c[0] and c[7] for c in commands_seen), "writes emitted")
            self.assertTrue(any(c[0] and c[8] and c[4] for c in commands_seen), "ACT commands emitted")
            self.assertTrue(any(sample["refresh_gnt"] for sample in trace), "refresh granted")
            self.assertTrue(any(not sample["timing_ready"][1] for sample in trace),
                            "tRC timing counter exercised")
            self.assertTrue(any(trace[c]["cmd"][0] for c in range(140, 155)),
                            "commands were active before the midstream reset")
            self.assertTrue(any(trace[c]["cmd"][0] for c in range(157, 190)),
                            "commands resumed after the midstream reset")

        run_simulation(dut, bench())

    def test_registered_mode_rejects_auto_precharge(self):
        settings = self._settings(True)
        settings.with_auto_precharge = True
        with self.assertRaisesRegex(AssertionError, "with_auto_precharge=False"):
            BankMachine(0, address_width=23, address_align=3, nranks=1,
                        settings=settings)


if __name__ == "__main__":
    unittest.main()
