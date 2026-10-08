# SPDX-License-Identifier: BSD-2-Clause
"""Check DDR4 refresh commands through the 1:2 DFI converter and CA packer."""

import unittest

from migen import *
from litex.gen.sim import run_simulation
from migen.fhdl.specials import Memory

from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.modules import EDY4016A
from litedram.phy.dfi import DDR4DFIMux, DFIRateConverter, Interface


SYS_HZ = 150e6
NATIVE_HZ = 300e6


def command_kind(cs_n, ras_n, cas_n, we_n, address):
    if cs_n:
        return None
    if not ras_n and cas_n and not we_n and (address & (1 << 10)):
        return "PREA"
    if not ras_n and not cas_n and we_n:
        return "REF"
    return "OTHER"


def make_dut():
    module = EDY4016A(SYS_HZ, "1:8", speedgrade="2400")
    phy = PhySettings(
        phytype="SyntheticDualSlotDFI", memtype="DDR4", databits=32,
        dfi_databits=64, nphases=8, nranks=1,
        rdphase=2, wrphase=3, cl=17, cwl=12, cmd_latency=5,
        read_latency=9, write_latency=1)
    settings = ControllerSettings(
        with_bank_group_interleaving=True, with_dual_slot=True,
        with_auto_precharge=False, with_refresh=True, refresh_postponing=1,
        with_registered_row_hit=True, with_registered_refresh_timers=True,
        with_registered_refresh_request=True, with_registered_timing_valid=True,
        with_activate_eligibility=True, read_time=32, write_time=16)
    controller = LiteDRAMController(
        phy, module.geom_settings, module.timing_settings, SYS_HZ,
        controller_settings=settings)

    native = Interface(17, 3, 1, 64, nphases=4)
    converter = DFIRateConverter(
        native, clkdiv="sys", clk="sys2x", ratio=2,
        preserve_throughput=True, serdes_reset=ResetSignal("sys"))
    mapped = Interface(17, 3, 1, 64, nphases=4)
    top = Module()
    top.clock_domains.cd_sys = ClockDomain("sys")
    top.clock_domains.cd_sys2x = ClockDomain("sys2x")
    top.submodules.controller = controller
    top.submodules.converter = converter
    top.submodules.ddr4_mux = DDR4DFIMux(native, mapped)
    top.comb += controller.dfi.connect(converter.dfi)
    top.comb += [phase.rddata.eq(0) for phase in native.phases]
    top.comb += [phase.rddata_valid.eq(0) for phase in native.phases]

    # This is the same CA packing equation as USNativeDDRPHY: one previous
    # phase-3 bit followed by each DFI phase repeated for its two DDR UI slots.
    ca_adr = Signal(17 * 8)
    ca_cs = Signal(8)
    for bit in range(17):
        field = ("we_n", "cas_n", "ras_n")[bit - 14] if bit >= 14 else "address"
        values = [getattr(phase, field)[0] if bit >= 14 else getattr(phase, field)[bit]
                  for phase in mapped.phases]
        previous = Signal(reset=1 if bit >= 15 else 0)
        top.sync.sys2x += previous.eq(values[3])
        top.comb += ca_adr[8*bit:8*(bit+1)].eq(
            Cat(previous, values[0], values[0], values[1], values[1],
                values[2], values[2], values[3]))
    cs_values = [phase.cs_n[0] for phase in mapped.phases]
    previous_cs = Signal(reset=1)
    top.sync.sys2x += previous_cs.eq(cs_values[3])
    top.comb += ca_cs.eq(Cat(previous_cs, cs_values[0], cs_values[0],
                             cs_values[1], cs_values[1], cs_values[2],
                             cs_values[2], cs_values[3]))
    return top, controller, converter, mapped, ca_adr, ca_cs, module


def run_case(sustained_traffic):
    top, controller, converter, mapped, ca_adr, ca_cs, module = make_dut()
    horizon = module.timing_settings.tREFI * 2 + module.timing_settings.tRFC + 40
    events = {"upstream": [], "native": [], "ca": [], "native_nonzero_phase": [],
              "ca_second_slot": []}
    interface = controller.interface

    def drive():
        switch_cycle = module.timing_settings.tREFI + 100
        events["switch_cycle"] = switch_cycle
        for bank in range(8):
            port = getattr(interface, f"bank{bank}")
            yield port.addr.eq(0)
            yield port.we.eq(0)
        for cycle in range(horizon):
            active = sustained_traffic and cycle >= switch_cycle
            for bank in range(8):
                yield getattr(interface, f"bank{bank}").valid.eq(int(active))
            yield interface.bank_read_ready.eq((1 << 8) - 1 if active else 0)
            yield interface.bank_write_ready.eq((1 << 8) - 1)
            for index, phase in enumerate(controller.dfi.phases):
                cs_n = (yield phase.cs_n)
                kind = command_kind(cs_n, (yield phase.ras_n),
                                    (yield phase.cas_n), (yield phase.we_n),
                                    (yield phase.address))
                if kind in ("PREA", "REF"):
                    events["upstream"].append((cycle, kind, index))
            yield

    def monitor_fast():
        # Converter latency is measured from controller DFI; event ordering is
        # compared by count/type, not by assuming a fixed phase offset.
        for cycle in range(2*horizon + 32):
            for index, phase in enumerate(mapped.phases):
                cs_n = (yield phase.cs_n)
                kind = command_kind(cs_n, (yield phase.ras_n),
                                    (yield phase.cas_n), (yield phase.we_n),
                                    (yield phase.address))
                if kind in ("PREA", "REF"):
                    item = (cycle, kind, index)
                    events["native"].append(item)
                    if index != 0:
                        events["native_nonzero_phase"].append(item)

            adr = (yield ca_adr)
            cs = (yield ca_cs)
            # CK rising-edge samples are slots 0,2,4,6. The packing repeats
            # each DFI phase over a pair of UI slots; count only CK rising slots.
            for slot in (0, 2, 4, 6):
                byte = sum(((adr >> (8*bit + slot)) & 1) << bit
                           for bit in range(17))
                cs_n = (cs >> slot) & 1
                kind = command_kind(cs_n, (byte >> 16) & 1,
                                    (byte >> 15) & 1, (byte >> 14) & 1,
                                    byte & 0x3fff)
                if kind in ("PREA", "REF"):
                    events["ca"].append((cycle, kind, slot))
                    if slot >= 4:
                        events["ca_second_slot"].append((cycle, kind, slot))
            yield

    # 6.666 ns sys and 3.333 ns native-fast periods, phase-aligned as on the
    # board. LiteX's integer-time simulator uses picoseconds here.
    fragment = top.get_fragment()
    for special in fragment.specials:
        if isinstance(special, Memory):
            for port in special.ports:
                if port.dat_r is None:
                    port.dat_r = Signal(special.width)
    run_simulation(fragment, {"sys": [drive()], "sys2x": [monitor_fast()]},
                   clocks={"sys": (6666, 3332), "sys2x": (3333, 1666)})
    return events, module


class DualSlotRefreshPipelineTest(unittest.TestCase):
    def check_case(self, sustained_traffic):
        events, module = run_case(sustained_traffic)
        upstream = events["upstream"]
        native = events["native"]
        ca = events["ca"]
        self.assertEqual(module.timing_settings.tREFI, 1172)
        self.assertEqual(module.timing_settings.tRFC, 40)
        refs = [cycle for cycle, kind, _ in upstream if kind == "REF"]
        self.assertGreaterEqual(len(refs), 2, upstream)
        self.assertTrue(any(cycle < events["switch_cycle"] for cycle in refs), refs)
        self.assertTrue(any(cycle >= events["switch_cycle"] for cycle in refs), refs)
        self.assertEqual([kind for _, kind, _ in native],
                         [kind for _, kind, _ in upstream])
        self.assertEqual([kind for _, kind, _ in ca],
                         [kind for _, kind, _ in upstream])
        self.assertTrue(all(phase == 0 for _, _, phase in upstream), upstream)
        self.assertTrue(all(phase == 0 for _, _, phase in native), native)
        self.assertEqual(events["native_nonzero_phase"], [])
        self.assertEqual(events["ca_second_slot"], [])
        self.assertTrue(all(cycle % 2 == 1 for cycle, _, _ in native), native)
        self.assertTrue(all(slot == 2 for _, _, slot in ca), ca)

        # PREA -> REF obeys tRP and REF -> following command obeys tRFC at the
        # source boundary; converted clocks have twice as many edges.
        for stream, scale in ((upstream, 1), (native, 2), (ca, 2)):
            for (cycle0, kind0, _), (cycle1, kind1, _) in zip(stream, stream[1:]):
                if kind0 == "PREA" and kind1 == "REF":
                    self.assertGreaterEqual(cycle1 - cycle0,
                                            scale*module.timing_settings.tRP)
                if kind0 == "REF":
                    self.assertGreaterEqual(cycle1 - cycle0,
                                            scale*module.timing_settings.tRFC)

    def test_idle_and_sustained_refresh_survive_converter_and_ca_mapping_once(self):
        self.check_case(sustained_traffic=True)


if __name__ == "__main__":
    unittest.main()
