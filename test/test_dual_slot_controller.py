#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Full controller/crossbar coverage for fixed DDR4 dual CAS slots."""

import unittest
from math import ceil
from collections import defaultdict

from migen import *
from litex.gen.sim import run_simulation

from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.frontend.paired import PairedPort
from litedram.modules import EDY4016A
from litedram.common import PhySettings


def run_controller_simulation(dut, generators):
    # The pinned simulator needs unused read wires on write-only FIFO ports.
    fragment = dut.get_fragment()
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, generators)


class TestDualSlotController(unittest.TestCase):
    def test_paired_full_path_data_timing_refresh_and_backpressure(self):
        self._run_full_path(with_auto_precharge=False)

    def test_paired_full_path_data_timing_refresh_and_backpressure_auto_precharge(self):
        self._run_full_path(with_auto_precharge=True)

    def _run_full_path(self, *, with_auto_precharge):
        frequency = 150e6
        module = EDY4016A(frequency, "1:8", speedgrade="2400")
        module.timing_settings.tCCD = 1
        module.timing_settings.tREFI = 120
        ck_rate = frequency * 8
        def minimum_ck(name):
            timing = module.get(name)
            return max(timing.ck, ceil(timing.ns * ck_rate / 1e9))

        trcd = minimum_ck("tRCD")
        trp = minimum_ck("tRP")
        tras = minimum_ck("tRAS")
        twr = minimum_ck("tWR")
        phy = PhySettings(
            phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
            dfi_databits=64, nphases=8, rdphase=2, wrphase=3, cl=17,
            cwl=12, cmd_latency=5, read_latency=9, write_latency=1, nranks=1)
        read_phase, write_phase = 2, 3
        # Exercise the production CSR form while keeping deterministic reset
        # phases for the synthetic board model.
        phy.rdphase = Signal(2, reset=read_phase)
        phy.wrphase = Signal(2, reset=write_phase)
        settings = ControllerSettings(
            with_bank_group_interleaving=True,
            with_dual_slot=True,
            with_auto_precharge=with_auto_precharge,
            with_refresh=True,
            refresh_postponing=1,
            with_registered_row_hit=True,
            with_registered_refresh_request=True,
            with_registered_timing_valid=True,
            with_activate_eligibility=True,
            read_time=32,
            write_time=16)
        controller = LiteDRAMController(
            phy, module.geom_settings, module.timing_settings, frequency,
            controller_settings=settings)
        bank_machines = [m for _, m in controller._submodules
                         if hasattr(m, "activate_ready")]
        top = Module()
        top.submodules.controller = controller
        crossbar = LiteDRAMCrossbar(controller.interface)
        top.submodules.crossbar = crossbar

        paired_write = PairedPort(
            [crossbar.get_port(mode="write") for _ in range(2)], "write", depth=8)
        paired_read = PairedPort(
            [crossbar.get_port(mode="read") for _ in range(2)], "read", depth=8)
        top.submodules.paired_write = paired_write
        top.submodules.paired_read = paired_read
        raw_write = crossbar.get_port(mode="write")
        raw_read = crossbar.get_port(mode="read")

        events = {
            "cycles": 0, "commands": [], "pair_write_cycles": [],
            "pair_read_cycles": [], "refresh": 0, "precharges": 0,
            "cas_counts": defaultdict(int), "memory": {}, "open_rows": {},
            "read_latches": [0, 0],
            "auto_close_due": {}, "implicit_precharges": [],
            "reads_due": {},
            "write_data_due": {},
            "maintenance_cycles": [],
            "pair_commands_done": False,
            "pair_data_done": False,
            "mixed_write_start": False, "mixed_write_cmd_done": False,
            "mixed_write_data_done": False, "mixed_read_start": False,
            "mixed_read_done": False, "cpu_read_waiting": False,
            "cpu_write_waiting": False, "write_during_cpu_read": False,
            "read_during_cpu_write": False,
            "model_errors": [],
        }
        dfi = controller.dfi

        @passive
        def synthetic_dfi_memory():
            while True:
                cycle = events["cycles"]
                cycle_ck = cycle * phy.nphases
                for bank, due_ck in list(events["auto_close_due"].items()):
                    if due_ck <= cycle_ck:
                        events["open_rows"].pop(bank, None)
                        events["implicit_precharges"].append(
                            (due_ck // phy.nphases, "AUTO_PRE", due_ck % phy.nphases,
                             bank, 1 << 10))
                        del events["auto_close_due"][bank]
                # Return full 256-bit data words at the configured DFI read
                # latency. The two physical slots have independent pipelines.
                due = events["reads_due"].pop(cycle, {})
                for slot in range(2):
                    phases = dfi.phases[slot*4:slot*4+4]
                    if slot in due:
                        events["read_latches"][slot] = due[slot]
                    value = events["read_latches"][slot]
                    for offset, phase in enumerate(phases):
                        yield phase.rddata.eq((value >> (64*offset)) & ((1 << 64)-1))
                        yield phase.rddata_valid.eq(int(slot in due))

                commands_this_cycle = []
                maintenance_this_cycle = False
                refresh_this_cycle = False
                for phase_index, phase in enumerate(dfi.phases):
                    cs_n = (yield phase.cs_n)
                    ras_n = (yield phase.ras_n)
                    cas_n = (yield phase.cas_n)
                    we_n = (yield phase.we_n)
                    bank = (yield phase.bank)
                    address = (yield phase.address)
                    slot = phase_index // 4
                    if not cs_n and not ras_n and cas_n and we_n:
                        maintenance_this_cycle = True
                        if bank in events["auto_close_due"]:
                            events["model_errors"].append((cycle,
                                "ACT before implicit auto-precharge recovery", bank))
                        if phase_index != 0:
                            events["model_errors"].append((cycle,
                                "ACT outside phase zero", phase_index))
                        events["open_rows"][bank] = address
                        events["commands"].append((cycle, "ACT", phase_index, bank, address))
                    elif not cs_n and not ras_n and cas_n and not we_n:
                        maintenance_this_cycle = True
                        if phase_index != 0:
                            events["model_errors"].append((cycle,
                                "PRE outside phase zero", phase_index))
                        events["precharges"] += 1
                        if address & (1 << 10):
                            events["open_rows"].clear()
                            events["auto_close_due"].clear()
                        else:
                            events["open_rows"].pop(bank, None)
                            events["auto_close_due"].pop(bank, None)
                        events["commands"].append((cycle, "PRE", phase_index, bank, address))
                    elif not cs_n and not ras_n and not cas_n and we_n:
                        maintenance_this_cycle = True
                        refresh_this_cycle = True
                        if phase_index != 0:
                            events["model_errors"].append((cycle,
                                "REF outside phase zero", phase_index))
                        events["refresh"] += 1
                        events["commands"].append((cycle, "REF", phase_index, bank, address))
                    elif not cs_n and ras_n and not cas_n:
                        kind = "WR" if not we_n else "RD"
                        row = events["open_rows"].get(bank)
                        if row is None:
                            events["model_errors"].append((cycle, "CAS without open row", bank))
                            row = 0
                        key = (bank, row, address & 0x3ff)
                        commands_this_cycle.append((slot, kind, bank, row, address, key))
                        events["commands"].append((cycle, kind, phase_index, bank, address))
                        if kind == "WR" and events["cpu_read_waiting"]:
                            events["write_during_cpu_read"] = True
                        if kind == "RD" and events["cpu_write_waiting"]:
                            events["read_during_cpu_write"] = True
                        if with_auto_precharge and (address & (1 << 10)):
                            cas_ck = cycle * phy.nphases + phase_index
                            recovery = max(tras, twr if kind == "WR" else 9)
                            events["auto_close_due"][bank] = cas_ck + recovery

                for slot, kind, bank, row, column, key in commands_this_cycle:
                    if kind == "RD":
                        word = events["memory"].get(key, 0)
                        due_cycle = cycle + phy.read_latency - 1
                        slot_words = events["reads_due"].setdefault(due_cycle, {})
                        if slot in slot_words:
                            events["model_errors"].append((cycle, "duplicate slot read", slot))
                        slot_words[slot] = word
                    else:
                        due_cycle = cycle + phy.write_latency
                        events["write_data_due"].setdefault((due_cycle, slot), []).append(key)

                # DFI write data launches write_latency controller cycles
                # after its CAS. Keep each command's address until that later
                # slot payload is visible on all four phases.
                for slot in range(2):
                    for key in events["write_data_due"].pop((cycle, slot), []):
                        phases = dfi.phases[slot*4:slot*4+4]
                        value = 0
                        byte_mask = 0
                        for i, phase_data in enumerate(phases):
                            value |= (yield phase_data.wrdata) << (64*i)
                            byte_mask |= (yield phase_data.wrdata_mask) << (8*i)
                        old = events["memory"].get(key, 0)
                        for byte in range(32):
                            if not (byte_mask >> byte) & 1:
                                mask = 0xff << (8*byte)
                                old = (old & ~mask) | (value & mask)
                        events["memory"][key] = old

                # Check every physical command and retain cadence evidence.
                cas = [item for item in commands_this_cycle]
                if maintenance_this_cycle:
                    events["maintenance_cycles"].append(cycle)
                    if cas:
                        events["model_errors"].append((cycle,
                            "maintenance overlapped CAS", cas))
                    if refresh_this_cycle and (events["reads_due"] or events["write_data_due"]):
                        events["model_errors"].append((cycle,
                            "refresh did not drain data pipelines"))
                events["cas_counts"][cycle] = len(cas)
                if len(cas) > 2:
                    events["model_errors"].append((cycle, "more than two CAS", len(cas)))
                if len(cas) == 2:
                    if cas[0][1] != cas[1][1] or cas[0][0] == cas[1][0]:
                        events["model_errors"].append((cycle, "invalid dual CAS pair", cas))
                    elif cas[0][1] == "WR":
                        events["pair_write_cycles"].append(cycle)
                    else:
                        events["pair_read_cycles"].append(cycle)
                events["cycles"] += 1
                yield

        def send_command(endpoint, address, *, we=0):
            yield endpoint.addr.eq(address)
            yield endpoint.we.eq(we)
            yield endpoint.valid.eq(1)
            yield
            for _ in range(600):
                if (yield endpoint.ready):
                    yield endpoint.valid.eq(0)
                    yield
                    return
                yield
            self.fail("native command timed out")

        def send_write_data(endpoint, data, mask):
            yield endpoint.data.eq(data)
            yield endpoint.we.eq(mask)
            yield endpoint.valid.eq(1)
            yield
            for _ in range(600):
                if (yield endpoint.ready):
                    yield endpoint.valid.eq(0)
                    yield
                    return
                yield
            state = {
                "cycle": events["cycles"],
                "refresh": (yield controller.refresher.cmd.valid),
                "fsm": (yield controller.multiplexer.fsm.state),
                "ready": (yield endpoint.ready),
                "banks": [],
            }
            for index, bm in enumerate(bank_machines):
                if (yield bm.cmd.valid):
                    state["banks"].append((index, (yield bm.cmd.is_read),
                        (yield bm.cmd.is_write), (yield bm.cmd.is_cmd),
                        (yield bm.cmd.ba), (yield bm.cmd.a)))
            self.fail("native write data timed out: {}".format(state))

        def receive(endpoint, expected, *, hold_cycles=0):
            for _ in range(600):
                if (yield endpoint.valid):
                    break
                yield
            else:
                self.fail("native read response timed out")
            for _ in range(hold_cycles):
                self.assertEqual((yield endpoint.data), expected)
                yield
            self.assertEqual((yield endpoint.data), expected)
            yield endpoint.ready.eq(1)
            yield
            yield endpoint.ready.eq(0)

        pair_addresses = list(range(12)) + [512, 513]
        mixed_write_addresses = list(range(1024, 1032))
        expected_pair = {
            address: ((0xA5A50000 + index) << 256) | (0x12340000 + index)
            for index, address in enumerate(pair_addresses)
        }
        expected_mixed = {
            address: ((0xC0DE0000 + index) << 256) | (0xFACE0000 + index)
            for index, address in enumerate(mixed_write_addresses)
        }

        def main():
            pw = paired_write.port
            pr = paired_read.port
            yield pr.rdata.ready.eq(0)
            while not (events["pair_commands_done"] and events["pair_data_done"]):
                yield
            for _ in range(600):
                if (yield paired_write.drained):
                    break
                yield
            else:
                self.fail("paired writes did not drain before raw-port traffic")
            for _ in range(80):
                yield

            # A standalone native master exercises partial byte enables. Its
            # readback confirms the slot data path applies active-high masks.
            partial_addr = 701
            original = int.from_bytes(bytes((i * 7 + 3) & 0xff for i in range(32)), "little")
            update = int.from_bytes(bytes((255 - i * 5) & 0xff for i in range(32)), "little")
            byte_enable = sum(1 << i for i in range(32) if i % 3 == 1)
            expected_raw = original
            for byte in range(32):
                if (byte_enable >> byte) & 1:
                    mask = 0xff << (byte * 8)
                    expected_raw = (expected_raw & ~mask) | (update & mask)
            yield from send_command(raw_write.cmd, partial_addr, we=1)
            yield from send_write_data(raw_write.wdata, original, (1 << 32) - 1)
            yield from send_command(raw_write.cmd, partial_addr, we=1)
            yield from send_write_data(raw_write.wdata, update, byte_enable)

            yield from send_command(raw_read.cmd, partial_addr)
            for _ in range(600):
                if (yield raw_read.rdata.valid):
                    break
                yield
            else:
                state = {
                    "cycle": events["cycles"],
                    "error": (yield crossbar.dual_slot_data.error),
                    "valid": (yield raw_read.rdata.valid),
                    "ready": (yield raw_read.rdata.ready),
                    "commands": events["commands"][-20:],
                    "banks": [],
                }
                for index, bm in enumerate(bank_machines):
                    state["banks"].append((index, (yield bm.cmd.valid),
                        (yield bm.cmd.is_read), (yield bm.cmd.is_write),
                        (yield bm.cmd.is_cmd), (yield bm.cmd.ba),
                        (yield bm.cmd.a), (yield bm.activate_ready)))
                self.fail("standalone native read timed out: {}".format(state))
            raw_data = (yield raw_read.rdata.data)
            self.assertEqual(raw_data, expected_raw)
            yield raw_read.rdata.ready.eq(1)
            yield
            yield raw_read.rdata.ready.eq(0)

            # Queue eight paired reads before consuming any result, filling
            # the native read-credit depth. Hold the first response to check
            # stable backpressure behavior.
            burst_addresses = pair_addresses[:8]
            for address in burst_addresses:
                yield from send_command(pr.cmd, address)
            yield
            for index, address in enumerate(pair_addresses):
                if index >= len(burst_addresses):
                    yield from send_command(pr.cmd, address)
                for _ in range(600):
                    if (yield pr.rdata.valid):
                        break
                    yield
                else:
                    self.fail("paired read response did not arrive")
                actual = (yield pr.rdata.data)
                self.assertEqual(actual, expected_pair[address],
                    "paired read mismatch addr={} got={:#x} expected={:#x} "
                    "memory={!r} recent={!r}".format(address, actual,
                        expected_pair[address], events["memory"],
                        events["commands"][-30:]))
                if index == 0:
                    held_data = (yield pr.rdata.data)
                    for _ in range(8):
                        self.assertEqual((yield pr.rdata.data), held_data)
                        yield
                yield pr.rdata.ready.eq(1)
                yield
                yield pr.rdata.ready.eq(0)
                yield

            # Mixed phase A: keep paired DMA-like writes in flight while an
            # independent native CPU-like read targets the preinitialized,
            # disjoint BG1 range above.
            events["mixed_write_start"] = True
            events["cpu_read_waiting"] = True
            yield from send_command(raw_read.cmd, partial_addr)
            for _ in range(600):
                if (yield raw_read.rdata.valid):
                    break
                yield
            else:
                self.fail("CPU read stalled during paired writes")
            self.assertEqual((yield raw_read.rdata.data), expected_raw)
            yield raw_read.rdata.ready.eq(1)
            yield
            yield raw_read.rdata.ready.eq(0)
            yield
            events["cpu_read_waiting"] = False
            for _ in range(600):
                if events["mixed_write_cmd_done"] and events["mixed_write_data_done"] and \
                        (yield paired_write.drained):
                    break
                yield
            else:
                self.fail("paired writes did not make progress beside CPU read")

            # Mixed phase B reverses directions: the paired reader consumes
            # the range just written while the CPU-like master writes BG1.
            events["cpu_write_waiting"] = True
            events["mixed_read_start"] = True
            cpu_update = int.from_bytes(bytes((0xD3 + i * 11) & 0xff
                for i in range(32)), "little")
            yield from send_command(raw_write.cmd, partial_addr, we=1)
            yield from send_write_data(raw_write.wdata, cpu_update, (1 << 32) - 1)
            events["cpu_write_waiting"] = False
            for _ in range(1200):
                if events["mixed_read_done"]:
                    break
                yield
            else:
                self.fail("paired reads did not make progress beside CPU write")
            self.assertTrue(events["write_during_cpu_read"],
                "paired writes did not progress while CPU read was outstanding")
            self.assertTrue(events["read_during_cpu_write"],
                "paired reads did not progress while CPU write was outstanding")

            # Drain commands, refresh, and read pipelines before checking the
            # physical command trace.
            for _ in range(500):
                if (yield paired_write.drained):
                    break
                yield
            for _ in range(50):
                yield
            self.assertEqual((yield paired_write.error), 0)
            self.assertEqual((yield paired_read.error), 0)
            self.assertEqual((yield crossbar.dual_slot_data.error), 0)
            self.assertEqual(events["memory"].get((6, 0, 752)), cpu_update)
            stored_words = set(events["memory"].values())
            for address in mixed_write_addresses:
                self.assertIn(expected_mixed[address] & ((1 << 256) - 1), stored_words,
                    "mixed paired low slot missing at {}".format(address))
                self.assertIn(expected_mixed[address] >> 256, stored_words,
                    "mixed paired high slot missing at {}".format(address))

        def paired_command_source():
            port = paired_write.port
            for address in pair_addresses:
                yield port.cmd.addr.eq(address)
                yield port.cmd.valid.eq(1)
                yield
                for _ in range(600):
                    if (yield port.cmd.ready):
                        break
                    yield
                else:
                    self.fail("paired command producer timed out")
            yield port.cmd.valid.eq(0)
            events["pair_commands_done"] = True
            while not events["mixed_write_start"]:
                yield
            for address in mixed_write_addresses:
                yield port.cmd.addr.eq(address)
                yield port.cmd.valid.eq(1)
                yield
                for _ in range(600):
                    if (yield port.cmd.ready):
                        break
                    yield
                else:
                    self.fail("mixed paired command timed out at {}".format(address))
            yield port.cmd.valid.eq(0)
            events["mixed_write_cmd_done"] = True

        def paired_data_source():
            port = paired_write.port
            yield port.wdata.we.eq((1 << 64) - 1)
            for index, address in enumerate(pair_addresses):
                yield port.wdata.data.eq(expected_pair[address])
                yield port.wdata.valid.eq(1)
                yield
                for _ in range(600):
                    if (yield port.wdata.ready):
                        break
                    yield
                else:
                    self.fail("paired data producer timed out")
            yield port.wdata.valid.eq(0)
            events["pair_data_done"] = True
            while not events["mixed_write_start"]:
                yield
            for address in mixed_write_addresses:
                yield port.wdata.data.eq(expected_mixed[address])
                yield port.wdata.valid.eq(1)
                yield
                for _ in range(600):
                    if (yield port.wdata.ready):
                        break
                    yield
                else:
                    self.fail("mixed paired data timed out at {}".format(address))
            yield port.wdata.valid.eq(0)
            events["mixed_write_data_done"] = True

        def paired_mixed_read_source():
            port = paired_read.port
            while not events["mixed_read_start"]:
                yield
            for address in mixed_write_addresses:
                yield from send_command(port.cmd, address)
                for _ in range(600):
                    if (yield port.rdata.valid):
                        break
                    yield
                else:
                    self.fail("mixed paired read timed out at {}".format(address))
                self.assertEqual((yield port.rdata.data), expected_mixed[address],
                    "mixed paired read mismatch at {}".format(address))
                if address == mixed_write_addresses[0]:
                    for _ in range(4):
                        self.assertEqual((yield port.rdata.data), expected_mixed[address])
                        yield
                yield port.rdata.ready.eq(1)
                yield
                yield port.rdata.ready.eq(0)
                yield
            events["mixed_read_done"] = True

        run_controller_simulation(top, [main(), synthetic_dfi_memory(),
                                        paired_command_source(), paired_data_source(),
                                        paired_mixed_read_source()])

        self.assertFalse(events["model_errors"], events["model_errors"][:8])
        self.assertGreater(len(events["pair_write_cycles"]), 0,
                           "no cycle issued both fixed write slots")
        self.assertGreater(len(events["pair_read_cycles"]), 0,
                           "no cycle issued both fixed read slots")
        # Sustained paired writes in one open row should issue at adjacent
        # slow-clock boundaries; this checks throughput, not just correctness.
        write_cycles = events["pair_write_cycles"]
        longest_run = run = 1
        for previous, current in zip(write_cycles, write_cycles[1:]):
            run = run + 1 if current == previous + 1 else 1
            longest_run = max(longest_run, run)
        self.assertGreaterEqual(longest_run, 4,
            "pair cycles={!r}, DFI commands={!r}".format(write_cycles,
                [c for c in events["commands"] if c[1] in ("WR", "RD")]))
        self.assertGreater(events["refresh"], 0)
        if with_auto_precharge:
            self.assertGreater(len(events["implicit_precharges"]), 0,
                "auto-precharge mode emitted no A10 auto-precharge CAS")
        else:
            self.assertGreaterEqual(events["precharges"], 2)

        # DFI phase index is the physical CK offset within the 8-phase
        # controller cycle. Check JEDEC bank timing independently of the DUT's
        # internal eligibility signals.
        last_activate = {}
        last_precharge = {}
        last_cas_by_group = {}
        last_read_ck = None
        last_write_ck = None
        previous_cas = None
        physical_commands = sorted(events["commands"] + events["implicit_precharges"],
            key=lambda command: command[0] * phy.nphases + command[2])
        for cycle, kind, phase_index, bank, address in physical_commands:
            ck = cycle * phy.nphases + phase_index
            if kind == "ACT":
                if bank in last_precharge:
                    self.assertGreaterEqual(ck - last_precharge[bank], trp,
                        "PRE->ACT too short for bank {}".format(bank))
                last_activate[bank] = ck
            elif kind in ("PRE", "AUTO_PRE"):
                pre_all = kind == "PRE" and (address & (1 << 10))
                if not pre_all and bank in last_activate:
                    self.assertGreaterEqual(ck - last_activate[bank], tras,
                        "ACT->PRE too short for bank {}".format(bank))
                if pre_all:
                    for active_bank, active_ck in last_activate.items():
                        self.assertGreaterEqual(ck - active_ck, tras,
                            "ACT->PRE-all too short for bank {}".format(active_bank))
                    for precharged_bank in range(8):
                        last_precharge[precharged_bank] = ck
                else:
                    last_precharge[bank] = ck
            elif kind in ("RD", "WR"):
                self.assertIn(bank, last_activate,
                              "CAS without prior ACT for bank {}".format(bank))
                self.assertGreaterEqual(ck - last_activate[bank], trcd,
                    "ACT->CAS too short for bank {}".format(bank))
                if kind == "RD":
                    if last_write_ck is not None:
                        twtr = minimum_ck("tWTR")
                        self.assertGreaterEqual(ck - last_write_ck,
                            phy.cwl + 4 + twtr, "WR->RD turnaround below WTR floor")
                    last_read_ck = ck
                else:
                    if last_read_ck is not None:
                        trtw = phy.cl + 8 - phy.cwl
                        self.assertGreaterEqual(ck - last_read_ck, trtw,
                            "RD->WR turnaround below RTW floor")
                    last_write_ck = ck
                group = bank // 4
                if group in last_cas_by_group:
                    self.assertGreaterEqual(ck - last_cas_by_group[group], 8,
                        "same-group CAS spacing below 8 CK")
                last_cas_by_group[group] = ck
                if previous_cas is not None:
                    previous_ck, previous_kind, previous_bank = previous_cas
                    if bank // 4 == previous_bank // 4:
                        self.assertGreaterEqual(ck - previous_ck, 8,
                            "same-group CAS spacing below 8 CK")
                    else:
                        self.assertGreaterEqual(ck - previous_ck, 4,
                            "different-group CAS spacing below 4 CK")
                    if previous_ck == ck:
                        self.assertEqual(kind, previous_kind,
                            "read and write CAS issued in the same CK")
                previous_cas = (ck, kind, bank)


if __name__ == "__main__":
    unittest.main()
