#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Paired DMA through the controller and the real 2:1 DFI converter."""

import unittest

from migen import *
from litex.gen.sim import run_simulation

from litedram.common import LiteDRAMNativePort, PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.frontend.dma import LiteDRAMDMAReader, LiteDRAMDMAWriter
from litedram.frontend.paired import PairedPort
from litedram.modules import EDY4016A
from litedram.phy.dfi import DFIRateConverter, Interface


class SmallEDY4016A(EDY4016A):
    nrows = 2


def run_controller_simulation(dut, generators):
    fragment = dut.get_fragment()
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, generators,
                   clocks={"sys": (8, 3), "sys2x": (4, 1)})


class TestDualSlotDMAConverterE2E(unittest.TestCase):
    beats = 32
    slot_bits = 256
    native_read_latency = 12

    def word(self, index):
        # Different, address-dependent patterns in each physical BL8 slot.
        slot_bytes = self.slot_bits // 8
        low = sum(((index * 17 + i * 29 + 3) & 0xff) << (8 * i)
                  for i in range(slot_bytes))
        high = sum(((index * 41 + i * 13 + 0xa7) & 0xff) << (8 * i)
                   for i in range(slot_bytes))
        return low | (high << self.slot_bits)

    def run_case(self, *, stale_upper=False, extra_slot_register=False,
                 profile="2400", data_bits=32, native_read_latency=12,
                 native_write_latency=3, controller_write_latency=1,
                 converter_write_delay=1, controller_read_latency=None,
                 beats=None):
        self.beats = type(self).beats if beats is None else beats
        self.slot_bits = data_bits * 8
        self.native_read_latency = native_read_latency
        if profile == "2667":
            frequency = 166666666.5
            cl, cwl, rdphase, wrphase = 19, 14, 0, 1
        else:
            frequency = 150e6
            cl, cwl, rdphase, wrphase = 17, 12, 2, 3
        module = SmallEDY4016A(frequency, "1:8", speedgrade="2400")
        module.geom_settings.bankbits = 3 if data_bits == 32 else 4
        if data_bits == 64:
            module.geom_settings.rowbits = 15
            module.geom_settings.colbits = 10
        module.geom_settings.addressbits = 17
        module.timing_settings.tCCD = 1
        module.timing_settings.tREFI = 100
        override_controller_latency = controller_read_latency is not None
        if controller_read_latency is None:
            controller_read_latency = native_read_latency//2 + 3
        controller_phy = PhySettings(
            phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=data_bits,
            dfi_databits=2*data_bits, nphases=8, nranks=1,
            rdphase=rdphase, wrphase=wrphase, cl=cl, cwl=cwl, cmd_latency=5,
            read_latency=controller_read_latency,
            write_latency=controller_write_latency)
        wrapped_phy = None
        if data_bits == 64:
            # Use production latency conversion rather than duplicating its
            # formula in the synthetic controller. The BFM supplies the raw
            # physical four-phase DFI on the same boundary as a real PHY.
            class BFMPhy(Module):
                def __init__(self, csr_cdc=None):
                    self.dfi = Interface(addressbits=module.geom_settings.addressbits,
                        bankbits=4, nranks=1, databits=128, nphases=4)
                    self.settings = PhySettings(
                        phytype="SyntheticNativeDFI", memtype="DDR4", databits=64,
                        dfi_databits=128, nphases=4, nranks=1,
                        rdphase=rdphase, wrphase=wrphase, cl=cl, cwl=cwl,
                        cmd_latency=5, read_latency=native_read_latency,
                        write_latency=native_write_latency)
            wrapped = DFIRateConverter.phy_wrapper(BFMPhy, 2,
                preserve_throughput=True, serdes_reset=ResetSignal("sys"))
            wrapped_phy = wrapped()
            controller_phy = wrapped_phy.settings
            if override_controller_latency:
                controller_phy.read_latency = controller_read_latency
        controller = LiteDRAMController(
            controller_phy, module.geom_settings, module.timing_settings, frequency,
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

        phy_dfi = Interface(addressbits=module.geom_settings.addressbits,
                            bankbits=module.geom_settings.bankbits, nranks=1,
                            databits=2*data_bits, nphases=4)
        # Keep the raw deserializer slot mapping under test. The two clocks
        # below have aligned first rising edges, as in the native CRG.
        if wrapped_phy is None:
            converter = DFIRateConverter(
                phy_dfi, clkdiv="sys", clk="sys2x", ratio=2,
                write_delay=converter_write_delay,
                read_delay=native_read_latency % 2, preserve_throughput=True,
                serdes_reset=ResetSignal("sys"),
                align_read_slots=extra_slot_register)
        else:
            converter = wrapped_phy
            phy_dfi = wrapped_phy.phy.dfi

        top = Module()
        top.clock_domains.cd_sys = ClockDomain("sys")
        top.clock_domains.cd_sys2x = ClockDomain("sys2x")
        top.submodules.controller = controller
        top.submodules.converter = converter
        top.comb += controller.dfi.connect(converter.dfi)
        crossbar = LiteDRAMCrossbar(controller.interface)
        top.submodules.crossbar = crossbar
        write_pair = PairedPort(
            [crossbar.get_port(mode="write") for _ in range(2)], "write", depth=8)
        read_pair = PairedPort(
            [crossbar.get_port(mode="read") for _ in range(2)], "read", depth=8)
        top.submodules.write_pair = write_pair
        top.submodules.read_pair = read_pair
        writer = LiteDRAMDMAWriter(write_pair.port, fifo_depth=8,
                                   fifo_buffered=True)
        reader = LiteDRAMDMAReader(read_pair.port, fifo_depth=8,
                                   fifo_buffered=True)
        top.submodules.writer = writer
        top.submodules.reader = reader

        memory = {}
        open_rows = {}
        writes_due = {}
        reads_due = {}
        stale_latches = [0] * (4 if data_bits == 64 else 2)
        read_latency_ledger = []
        stale_upper_previous_nonzero = [False]
        metrics = {"fast_cycle": 0, "write_commands": 0,
                   "read_commands": 0, "refreshes": 0,
                   "write_events": 0, "read_events": 0,
                   "write_error": 0, "read_error": 0, "data_error": 0}
        data_errors = []
        read_count = [0]
        expected = [self.word(i) for i in range(self.beats)]

        @passive
        def fast_dfi_memory():
            while True:
                cycle = metrics["fast_cycle"]
                due = reads_due.pop(cycle, [])
                for key, bank_group, command_cycle in due:
                    current_value = memory.get(key, 0)
                    value = current_value
                    if stale_upper and (bank_group & 1):
                        value = stale_latches[bank_group]
                        stale_upper_previous_nonzero[0] |= stale_latches[bank_group] != 0
                    # The injected fault returns the previous response, then
                    # advances the latch with this command's current memory
                    # word, matching a one-beat stale-data defect.
                    stale_latches[bank_group] = current_value
                    metrics["read_events"] += 1
                    # Due entries are consumed at this loop's current cycle.
                    # These assignments reach the next sys2x sampling edge,
                    # so a command sampled at command_cycle observes data at
                    # command_cycle + 12 when the native PHY latency is 12.
                    visible_edge = cycle + 1
                    read_latency_ledger.append(
                        (command_cycle, visible_edge, self.native_read_latency))
                    for phase_index, phase in enumerate(phy_dfi.phases):
                        yield phase.rddata.eq((value >> (2*data_bits * phase_index)) & ((1 << (2*data_bits)) - 1))
                        yield phase.rddata_valid.eq(1)
                if not due:
                    for phase in phy_dfi.phases:
                        yield phase.rddata_valid.eq(0)

                for phase_index, phase in enumerate(phy_dfi.phases):
                    cs_n = (yield phase.cs_n)
                    ras_n = (yield phase.ras_n)
                    cas_n = (yield phase.cas_n)
                    we_n = (yield phase.we_n)
                    bank = (yield phase.bank)
                    address = (yield phase.address)
                    if not cs_n and not ras_n and cas_n and we_n:
                        open_rows[bank] = address
                    elif not cs_n and not ras_n and cas_n and not we_n:
                        if address & (1 << 10):
                            open_rows.clear()
                        else:
                            open_rows.pop(bank, None)
                    elif not cs_n and not ras_n and not cas_n and we_n:
                        open_rows.clear()
                        metrics["refreshes"] += 1
                    elif not cs_n and ras_n and not cas_n:
                        kind = "WR" if not we_n else "RD"
                        # The converter serializes the two four-phase slots
                        # onto alternating sys2x cycles after shared reset.
                        bank_group = bank >> 2
                        row = open_rows.get(bank)
                        if row is None:
                            self.fail("CAS without ACT at fast cycle {}".format(cycle))
                        key = (bank, row, address & 0x3ff)
                        if kind == "WR":
                            writes_due.setdefault(cycle + native_write_latency,
                                                  []).append((key, bank_group))
                            metrics["write_commands"] += 1
                        else:
                            # Native PHY latency is in sys2x cycles. The BFM
                            # samples commands at this edge and starts driving
                            # the response on the edge immediately before its
                            # due sampling edge, hence latency - 1 here.
                            due_cycle = cycle + self.native_read_latency - 1
                            reads_due.setdefault(due_cycle, []).append(
                                (key, bank_group, cycle))
                            metrics["read_commands"] += 1

                for key, slot in writes_due.pop(cycle, []):
                    value = mask_bits = 0
                    for phase_index, phase in enumerate(phy_dfi.phases):
                        value |= (yield phase.wrdata) << (2*data_bits * phase_index)
                        mask_bits |= (yield phase.wrdata_mask) << ((2*data_bits // 8) * phase_index)
                    old = memory.get(key, 0)
                    for byte in range(self.slot_bits // 8):
                        if not ((mask_bits >> byte) & 1):
                            byte_mask = 0xff << (8 * byte)
                            old = (old & ~byte_mask) | (value & byte_mask)
                    memory[key] = old
                    metrics["write_events"] += 1
                metrics["fast_cycle"] += 1
                yield

        def write_source():
            for index, value in enumerate(expected):
                yield writer.sink.address.eq(index)
                yield writer.sink.data.eq(value)
                yield writer.sink.last.eq(index == self.beats - 1)
                yield writer.sink.valid.eq(1)
                while True:
                    yield
                    if (yield writer.sink.ready):
                        break
            yield writer.sink.valid.eq(0)
            for _ in range(3000):
                if (yield write_pair.drained):
                    break
                yield
            else:
                self.fail("paired write data did not drain")
            yield writer_done.eq(1)

        def read_source():
            while not (yield writer_done):
                yield
            for index in range(self.beats):
                yield reader.sink.address.eq(index)
                yield reader.sink.last.eq(index == self.beats - 1)
                yield reader.sink.valid.eq(1)
                while True:
                    yield
                    if (yield reader.sink.ready):
                        break
            yield reader.sink.valid.eq(0)

        writer_done = Signal()

        def read_monitor():
            yield reader.source.ready.eq(1)
            for _ in range(12000):
                if (yield reader.source.valid):
                    actual = (yield reader.source.data)
                    index = read_count[0]
                    if index >= self.beats or actual != expected[index]:
                        data_errors.append((index, actual,
                            None if index >= self.beats else expected[index]))
                    read_count[0] += 1
                if read_count[0] >= self.beats:
                    return
                yield
            self.fail("paired DMA read stream timed out")

        @passive
        def error_monitor():
            while True:
                metrics["write_error"] = (yield write_pair.error)
                metrics["read_error"] = (yield read_pair.error)
                metrics["data_error"] = (yield crossbar.dual_slot_data.error)
                yield

        run_controller_simulation(top, {
            "sys": [write_source(), read_source(), read_monitor(), error_monitor()],
            "sys2x": [fast_dfi_memory()],
        })
        self.assertEqual(read_count[0], self.beats)
        self.assertEqual(metrics["write_events"], self.beats * 2)
        self.assertEqual(metrics["read_events"], self.beats * 2)
        self.assertTrue(all(visible - issued == latency
                            for issued, visible, latency in read_latency_ledger))
        if self.beats >= 16:
            self.assertGreater(metrics["refreshes"], 0)
        self.assertEqual(metrics["write_error"], 0)
        self.assertEqual(metrics["read_error"], 0)
        self.assertEqual(metrics["data_error"], 0)
        if stale_upper or extra_slot_register:
            self.assertTrue(data_errors, "stale upper-slot response escaped the scoreboard")
            lower_mask = (1 << self.slot_bits) - 1
            self.assertTrue(all(((actual ^ wanted) & lower_mask) == 0
                                and ((actual ^ wanted) >> self.slot_bits) != 0
                                for _, actual, wanted in data_errors),
                "negative control must corrupt only the upper 256-bit slot")
        else:
            self.assertEqual(data_errors, [], "first mismatch (index, actual, expected): {}".format(
                None if not data_errors else (data_errors[0][0],
                    hex(data_errors[0][1]), hex(data_errors[0][2]))))
        if stale_upper:
            self.assertTrue(stale_upper_previous_nonzero[0],
                "negative control never returned a nonzero previous upper-slot beat")

    def test_controller_converter_paired_dma_roundtrip(self):
        self.run_case(stale_upper=False)

    def test_controller_converter_paired_dma_roundtrip_2667_profile(self):
        self.run_case(stale_upper=False, profile="2667")

    def test_x64_paired_dma_roundtrip_even_native_read_latency(self):
        self.run_case(stale_upper=False, data_bits=64, native_read_latency=16,
                      native_write_latency=4, controller_write_latency=2,
                      converter_write_delay=0, beats=4)

    def test_x64_paired_dma_roundtrip_odd_native_read_latency(self):
        self.run_case(stale_upper=False, data_bits=64, native_read_latency=13,
                      native_write_latency=4, controller_write_latency=2,
                      converter_write_delay=0, beats=4)

    def test_x64_paired_dma_roundtrip_later_native_fifo_pop(self):
        self.run_case(stale_upper=False, data_bits=64, native_read_latency=18,
                      native_write_latency=4, controller_write_latency=2,
                      converter_write_delay=0, beats=4)

    def test_x64_paired_dma_2667_odd_read_and_write_latency(self):
        self.run_case(stale_upper=False, profile="2667", data_bits=64,
                      native_read_latency=13, native_write_latency=3, beats=4)

    def test_x64_early_owner_tag_is_detected(self):
        with self.assertRaisesRegex(AssertionError, "first mismatch"):
            self.run_case(data_bits=64, native_read_latency=13,
                native_write_latency=4, controller_write_latency=2,
                converter_write_delay=0, controller_read_latency=9, beats=4)

    def test_scoreboard_detects_stale_upper_slot_response(self):
        self.run_case(stale_upper=True)

    def test_extra_converter_slot_register_exposes_upper_slot_boundary_error(self):
        self.run_case(extra_slot_register=True)


if __name__ == "__main__":
    unittest.main()
