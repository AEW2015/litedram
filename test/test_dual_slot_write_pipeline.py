#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

from functools import reduce
from operator import or_
import random
from migen import *
from litex.gen.sim import run_simulation
from litedram.common import TappedDelayLine, BitSlip
from litedram.phy.dfi import Interface, DFIRateConverter

class DUT(Module):
    def __init__(self, pipeline, launch_alignment="active"):
        self.clock_domains.cd_sys = ClockDomain('sys')
        self.clock_domains.cd_sys2x = ClockDomain('sys2x')
        self.fast = f = Interface(17, 3, 1, 64, nphases=4)
        self.submodules.converter = c = DFIRateConverter(f, clkdiv='sys', clk='sys2x', ratio=2,
            write_delay=1, read_delay=0, preserve_throughput=True, serdes_reset=ResetSignal('sys'))
        self.accept = [Signal() for _ in range(2)]
        self.tags = [Signal(17) for _ in range(2)]
        self.data = [Signal(256) for _ in range(2)]
        self.mask = [Signal(32) for _ in range(2)]
        for slot in range(2):
            phases = c.dfi.phases[4*slot:4*slot+4]
            self.sync += [phases[3].wrdata_en.eq(self.accept[slot]), phases[3].address.eq(self.tags[slot])]
            dp = TappedDelayLine(self.data[slot], ntaps=2)
            mp = TappedDelayLine(self.mask[slot], ntaps=2)
            self.submodules += dp, mp
            self.comb += [Cat(*[p.wrdata for p in phases]).eq(dp.output),
                          Cat(*[p.wrdata_mask for p in phases]).eq(mp.output)]
        wr = ClockDomainsRenamer('sys2x')(TappedDelayLine(reduce(or_, [p.wrdata_en for p in f.phases]), ntaps=4))
        tag = ClockDomainsRenamer('sys2x')(TappedDelayLine(f.phases[3].address,
            ntaps=4 if launch_alignment == "pre" else 5))
        self.submodules += wr, tag
        self.tag = tag.output
        # These are conditional digital models of where the first sample is
        # anchored: one fast-clock register before the active internal OE
        # (tap 2), or at active OE (tap 3). They do not model DDR CK/CWL/pad
        # timing and cannot qualify a production pipeline choice.
        launch = {"pre": wr.taps[2], "active": wr.output}[launch_alignment]
        dqs = ClockDomainsRenamer('sys2x')(BitSlip(8, i=Mux(launch, 0x55, 0)))
        self.submodules += dqs
        self.dqs = dqs.o
        data = Cat(*[p.wrdata for p in f.phases])
        mask = Cat(*[p.wrdata_mask for p in f.phases])
        if pipeline:
            dp = ClockDomainsRenamer('sys2x')(TappedDelayLine(data, ntaps=pipeline))
            mp = ClockDomainsRenamer('sys2x')(TappedDelayLine(mask, ntaps=pipeline))
            self.submodules += dp, mp
            data, mask = dp.output, mp.output
        # At reset slip, each real 8-bit DQ/DM BitSlip is one register; vector
        # slips here have exactly the same zero-slip latency and preserve all bits.
        tx = ClockDomainsRenamer('sys2x')(BitSlip(256, i=data))
        tm = ClockDomainsRenamer('sys2x')(BitSlip(32, i=mask))
        self.submodules += tx, tm
        self.tx, self.tm = tx.o, tm.o

def trial(pipeline, launch_alignment="active", aligned_rising_edges=False):
    dut = DUT(pipeline, launch_alignment)
    work = {cycle:(cycle*2+1,cycle*2+2) for cycle in range(4,12)}
    work.update({14:(101,None),17:(None,102),21:(103,104)})
    values = {t:random.Random(t).getrandbits(256) for pair in work.values() for t in pair if t}
    masks = {t:random.Random(t+1000).getrandbits(32) for t in values}
    observed, errors = [], []
    def drive():
        for cycle in range(40):
            yield dut.cd_sys.rst.eq(cycle < 2)
            for slot, tag in enumerate(work.get(cycle,(None,None))):
                yield dut.accept[slot].eq(tag is not None)
                yield dut.tags[slot].eq(tag or 0)
                yield dut.data[slot].eq(values.get(tag,0))
                yield dut.mask[slot].eq(masks.get(tag,0))
            yield
    def monitor():
        scoring_started = False
        for cycle in range(90):
            if (yield dut.dqs) == 0x55:
                tag = (yield dut.tag)
                if tag in values:
                    scoring_started = True
                    observed.append(tag)
                    if (yield dut.tx) != values[tag] or (yield dut.tm) != masks[tag]:
                        errors.append((cycle,tag))
                elif scoring_started:
                    # Once the first transaction is seen, every DQS event
                    # must carry a known tag and appear in the exact sequence.
                    observed.append(tag)
                    errors.append((cycle,tag))
                # Before the first tagged transaction, ignore BitSlip fill.
            yield
    # sys phase 1 and sys2x phase 1 start at different rising edges (3 vs 1).
    # Phase 3 makes their first rising edges coincide at time 1.
    sys_phase = 3 if aligned_rising_edges else 1
    run_simulation(dut, {'sys':drive(), 'sys2x':monitor()}, clocks={'sys':(8,sys_phase),'sys2x':(4,1)})
    expected = [t for pair in work.values() for t in pair if t]
    assert observed == expected, (observed,expected)
    return errors

if __name__ == '__main__':
    for pipeline in range(5):
        errors = trial(pipeline)
        print('pipeline',pipeline,'errors',len(errors), errors[:3], flush=True)

def test_active_anchor_pipeline_preserves_payloads_and_masks():
    assert trial(1) == []


def test_active_anchor_single_slot_pipeline_is_not_reused_for_full_rate_writes():
    # Distinct adjacent bursts expose a two-fast-cycle payload displacement.
    assert len(trial(3)) == 20


def test_active_oe_register_latency_alignment_is_conditional():
    # With the first sample anchored at active internal OE (tap 3), tap3 plus
    # BitSlip's register latency aligns the payload and mask at pipeline 1.
    assert trial(1, "active") == []
    assert len(trial(0, "active")) == 20


def test_pre_oe_register_latency_alignment_is_conditional():
    # Moving the modeled sample anchor back to tap 2 changes the conditional
    # register alignment by one fast clock. Distinct bursts and masks make
    # displaced payloads observable. This is not a statement about physical
    # DDR sampling or which pipeline hardware should use.
    assert trial(0, "pre") == []
    assert len(trial(1, "pre")) == 20


def test_aligned_clock_active_anchor_register_latency_is_conditional():
    # sys and sys2x first rising edges coincide (phase 3 and phase 1).
    assert trial(1, "active", aligned_rising_edges=True) == []
    assert len(trial(0, "active", aligned_rising_edges=True)) == 20


def test_aligned_clock_pre_oe_register_latency_is_conditional():
    assert trial(0, "pre", aligned_rising_edges=True) == []
    assert len(trial(1, "pre", aligned_rising_edges=True)) == 20
