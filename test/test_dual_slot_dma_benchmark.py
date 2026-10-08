#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Native 512-bit benchmark roundtrip through paired dual-slot DDR4."""

import unittest

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.frontend.native_benchmark import NativeDMABenchmark
from litedram.frontend.paired import PairedPort
from litedram.modules import EDY4016A
from test.common import timeout_generator


class SmallEDY4016A(EDY4016A):
    nrows = 2


def run_controller_simulation(dut, generators):
    fragment = dut.get_fragment()
    # This Migen version needs read wires on write-only FIFO ports.
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, generators)


class TestDualSlotDMABenchmark(unittest.TestCase):
    def test_paired_native_dma_write_read_roundtrip(self):
        frequency = 150e6
        module = SmallEDY4016A(frequency, "1:8", speedgrade="2400")
        module.geom_settings.addressbits = 17
        module.timing_settings.tCCD = 1
        module.timing_settings.tREFI = 120
        phy_settings = PhySettings(
            phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
            dfi_databits=64, nphases=8, nranks=1,
            rdphase=2, wrphase=3, cl=17, cwl=12, cmd_latency=5,
            read_latency=9, write_latency=1)
        controller = LiteDRAMController(
            phy_settings, module.geom_settings, module.timing_settings, frequency,
            controller_settings=ControllerSettings(
                with_bank_group_interleaving=True,
                with_dual_slot=True,
                with_auto_precharge=False,
                with_refresh=True,
                refresh_postponing=1,
                with_registered_row_hit=True,
                with_registered_refresh_request=True,
                with_registered_timing_valid=True,
                with_activate_eligibility=True,
                read_time=32,
                write_time=16))

        top = Module()
        top.submodules.controller = controller
        crossbar = LiteDRAMCrossbar(controller.interface)
        top.submodules.crossbar = crossbar
        write_pair = PairedPort(
            [crossbar.get_port(mode="write") for _ in range(2)], "write", depth=8)
        read_pair = PairedPort(
            [crossbar.get_port(mode="read") for _ in range(2)], "read", depth=8)
        top.submodules.write_pair = write_pair
        top.submodules.read_pair = read_pair
        benchmark = NativeDMABenchmark(
            write_pair.port, read_pair.port, capacity=4096,
            fifo_depth=8, drained=write_pair.drained, databits=32)
        top.submodules.benchmark = benchmark

        # Model each fixed slot's return latency independently, matching
        # the controller-level DFI scoreboard's CAS+read_latency-1 convention.
        metrics = {"cycle": 0, "paired_cas_cycles": 0,
                   "pair_write_cas": 0, "pair_read_cas": 0,
                   "write_commits": 0, "first_read_write_commits": None,
                   "first_read_outstanding_writes": None}
        memory = {}
        open_rows = {}
        read_latches = [0, 0]
        reads_due = {}
        writes_due = {}

        @passive
        def dfi_memory():
            while True:
                cycle = metrics["cycle"]
                due = reads_due.pop(cycle, {})
                for slot in range(2):
                    if slot in due:
                        read_latches[slot] = due[slot]
                    for index, phase in enumerate(controller.dfi.phases[slot*4:slot*4+4]):
                        yield phase.rddata.eq((read_latches[slot] >> (64*index)) & ((1 << 64)-1))
                        yield phase.rddata_valid.eq(int(slot in due))

                commands = []
                for phase_index, phase in enumerate(controller.dfi.phases):
                    cs_n = (yield phase.cs_n)
                    ras_n = (yield phase.ras_n)
                    cas_n = (yield phase.cas_n)
                    we_n = (yield phase.we_n)
                    bank = (yield phase.bank)
                    address = (yield phase.address)
                    slot = phase_index // 4
                    if not cs_n and not ras_n and cas_n and we_n:
                        open_rows[bank] = address
                    elif not cs_n and not ras_n and cas_n and not we_n:
                        if address & (1 << 10):
                            open_rows.clear()
                        else:
                            open_rows.pop(bank, None)
                    elif not cs_n and not ras_n and not cas_n and we_n:
                        open_rows.clear()
                    elif not cs_n and ras_n and not cas_n:
                        kind = "WR" if not we_n else "RD"
                        row = open_rows.get(bank)
                        if row is None:
                            self.fail("CAS without ACT: bank={} cycle={}".format(bank, cycle))
                        key = (bank, row, address & 0x3ff)
                        commands.append((slot, kind, key))
                        if kind == "RD":
                            if metrics["first_read_write_commits"] is None:
                                metrics["first_read_write_commits"] = metrics["write_commits"]
                                metrics["first_read_outstanding_writes"] = sum(
                                    len(pending) for pending in writes_due.values())
                            reads_due.setdefault(cycle + phy_settings.read_latency - 1, {})[slot] = memory.get(key, 0)
                        else:
                            writes_due.setdefault((cycle + phy_settings.write_latency, slot), []).append(key)

                for slot in range(2):
                    for key in writes_due.pop((cycle, slot), []):
                        phases = controller.dfi.phases[slot*4:slot*4+4]
                        value = mask_bits = 0
                        for index, phase in enumerate(phases):
                            value |= (yield phase.wrdata) << (64*index)
                            mask_bits |= (yield phase.wrdata_mask) << (8*index)
                        old = memory.get(key, 0)
                        for byte in range(32):
                            if not ((mask_bits >> byte) & 1):
                                mask = 0xff << (8*byte)
                                old = (old & ~mask) | (value & mask)
                        memory[key] = old
                        metrics["write_commits"] += 1

                writes = sum(kind == "WR" for _, kind, _ in commands)
                reads = sum(kind == "RD" for _, kind, _ in commands)
                if writes + reads == 2:
                    metrics["paired_cas_cycles"] += 1
                    metrics["pair_write_cas"] += writes
                    metrics["pair_read_cas"] += reads
                metrics["cycle"] += 1
                yield

        def run_benchmark():
            yield benchmark.allowed.eq(1)
            yield benchmark._random.storage.eq(1)
            yield benchmark._base.storage.eq(0)
            yield benchmark._length.storage.eq(16 * 64)
            yield benchmark._timeout.storage.eq(20000)
            yield benchmark._start.re.eq(1)
            yield
            yield benchmark._start.re.eq(0)
            for _ in range(20000):
                if (yield benchmark._done.status):
                    break
                yield
            self.assertEqual((yield benchmark._done.status), 1)
            self.assertEqual((yield benchmark._fault.status), 0)
            self.assertEqual((yield benchmark._errors.status), 0)
            self.assertEqual((yield benchmark._write_beats.status), 16)
            self.assertEqual((yield benchmark._read_beats.status), 16)
            self.assertEqual((yield write_pair.error), 0)
            self.assertEqual((yield read_pair.error), 0)
            self.assertEqual((yield crossbar.dual_slot_data.error), 0)
            # The adapter's drained signal covers its own command/data queues;
            # the benchmark's settle interval must also let controller-accepted
            # writes reach the simulated DRAM before the read phase starts.
            self.assertEqual(metrics["first_read_write_commits"], 32)
            self.assertEqual(metrics["first_read_outstanding_writes"], 0)
            self.assertEqual(metrics["write_commits"], 32)

        run_controller_simulation(top, [run_benchmark(), dfi_memory(),
                                        timeout_generator(22000)])
        print("dual-slot NativeDMABenchmark simulation: beats=16, cycles={}, "
              "paired-CAS-cycles={}, write-CAS={}, read-CAS={}".format(
            metrics["cycle"], metrics["paired_cas_cycles"],
            metrics["pair_write_cas"], metrics["pair_read_cas"]))
        self.assertGreater(metrics["paired_cas_cycles"], 0)
        self.assertGreater(metrics["pair_write_cas"], 0)
        self.assertGreater(metrics["pair_read_cas"], 0)


if __name__ == "__main__":
    unittest.main()
