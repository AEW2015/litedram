# Isolated V22: group-local lane-valid capture plus registered group reductions.
"""Experimental DDR4 DFI adapter for public PHY Lite with A5 GPIO.

PHY Lite uses the unpatched public groups and independent command strobes.
All widths/order derive from generated public-750 evidence. Hardware training
is required before these default delays can be considered calibrated.
"""
import json
from pathlib import Path
from functools import reduce
from operator import or_
from migen import *
from litedram.common import PhySettings
from litedram.phy.dfi import Interface, DDR4DFIMux
from litedram_phy.fullrate_dfi_output_pipeline_v2 import DFIOutputPipeline
from litedram_phy.fullrate_phy_diagnostic_pipeline_v1 import PhyDiagnosticValidPipeline

class Agilex5Native2666FreshPHY(Module):
    def __init__(self, csr_cdc=None, cl=20, cwl=14, pipelined_diagnostics=False, read_latency=22):
        assert (cl,cwl)==(20,14) and read_latency>=1
        # Preserve the calibrated command-to-data sample positions when
        # changing the DRAM mode-register latencies. All profiles keep the
        # request in the first four phases of the slow eight-phase DFI.
        # CL20 moves the physical return one fast frame later; reserve a
        # further slow cycle while retaining the converter data slice.
        # Payload lead stays two fast edges; firmware calibrates the pin gate.
        rdphase = (3 - (cl - 12)) % 4
        wrphase = (1 - (cwl - 11)) % 4
        self.settings=PhySettings(phytype='Agilex5Native2666FreshV1',memtype='DDR4',
            databits=32,dfi_databits=64,nranks=1,nphases=4,rdphase=rdphase,wrphase=wrphase,
            cl=cl,cwl=cwl,read_latency=read_latency,write_latency=2)
        self.addressbits=17; self.bankbits=4
        self.dfi=Interface(17,4,1,64,4)
        backend=Interface(17,4,1,64,4)
        self.backend_dfi=backend
        physical=Interface(17,4,1,64,4)
        self.physical_dfi=physical
        # Expose the unchanged physical DFI endpoint for paired verification.
        self.physical_dfi=physical
        self.submodules.command_mux=DDR4DFIMux(backend,physical)
        self.submodules.dfi_output_pipeline=DFIOutputPipeline(self.dfi,backend)
        self.settings.read_latency += 2
        self.locked=Signal()
        self.read_en_mask=Signal(4,reset=15)
        read_mask_fast=Signal(4,reset=15)
        read_mask_fast.attr.add("no_retiming")
        self.sync += read_mask_fast.eq(self.read_en_mask)
        self.rd_delay=Signal(4,reset=2)
        self.wr_delay=Signal(4,reset=2)
        # CSR taps may originate in a slower domain. Sample them into the PHY
        # domain before using them as mux selectors, and keep Quartus from
        # retiming these control registers through the output datapath.
        rd_delay_fast=Signal(4,reset=2,name='rd_delay_fast')
        wr_delay_fast=Signal(4,reset=2,name='wr_delay_fast')
        rd_delay_fast.attr.add('no_retiming')
        wr_delay_fast.attr.add('no_retiming')
        self.sync += [rd_delay_fast.eq(self.rd_delay),wr_delay_fast.eq(self.wr_delay)]
        self.clear_capture=Signal()
        self.capture_mask=Signal(16)
        self.capture_count=Signal(32)
        self.mask_error=Signal()
        self.groups=[]
        for g in range(4):
            group=Record([('data_out',64),('data_in',64),('valid',4),('read_en',4),('oe',4),('strobe',8),('strobe_oe',4)],name=f'g{g}')
            # This is the existing output stage between the selected write-data
            # tap and the HIPI pins. Keep it as a real fabric register and do
            # not allow Quartus to absorb or retime it across the I/O boundary.
            group.data_out.attr.add('preserve')
            group.data_out.attr.add('no_retiming')
            for signal in (group.read_en, group.oe, group.strobe, group.strobe_oe):
                signal.attr.add('preserve')
                signal.attr.add('no_retiming')
            self.groups.append(group)
        self.cmd4=Signal(64);self.cmd5=Signal(144)
        self.cmd4_strobe=Signal(8);self.cmd5_strobe=Signal(8)
        for signal in (self.cmd4, self.cmd5, self.cmd4_strobe, self.cmd5_strobe):
            signal.attr.add('preserve')
            signal.attr.add('no_retiming')
        self.a5gpio=Signal(reset_less=True);self.a5delay=Signal(3,reset=2)
        self.a5gpio.attr.add('preserve')
        self.a5gpio.attr.add('no_retiming')
        self.command_conflict=Signal()
        # LiteDRAM presents the write-request lead two sys2x edges ahead of
        # its DFI payload beat. `wr_delay=2` therefore selects the current
        # payload, while larger taps delay the complete 256-bit beat relative
        # to that request without disturbing the existing output register.
        # Capture local partial tap selections on the same edge as the
        # frontend-to-backend complete-frame register. The histories below
        # are sourced from the raw frontend, so each branch at edge N matches
        # the old selector plus backend-data history consumed by the V19 pin
        # output register at edge N+1.
        front_write_data=Signal(256,name='front_write_data')
        front_write_history=[Signal(256,name=f'front_write_history_{i}') for i in range(13)]
        self.sync += front_write_history[0].eq(front_write_data)
        for i in range(1,len(front_write_history)):
            self.sync += front_write_history[i].eq(front_write_history[i-1])
        # Delays 0 and 1 are unsupported and intentionally saturate at the
        # minimum supported tap 2. Configured/trained taps are 2..6.
        effective_wr_delay=Mux(wr_delay_fast<2,2,wr_delay_fast)
        # Predecode in parallel with wr_delay_fast, preserving its exact
        # control latency. Invalid delay 0/1 saturates at tap 0 as before.
        write_taps=[front_write_data]+front_write_history
        tap_select=[self.wr_delay <= 2]+[self.wr_delay == i+2
            for i in range(1,len(write_taps))]
        write_tap_branches=[]
        for pair in range(len(write_taps)//2):
            branch=Signal(256,name=f'write_tap_branch_{pair}')
            branch.attr.add('preserve_syn_only')
            write_tap_branches.append(branch)
            self.sync += branch.eq(
                Mux(tap_select[2*pair],write_taps[2*pair],0) |
                Mux(tap_select[2*pair+1],write_taps[2*pair+1],0))
        self.write_tap_branches=write_tap_branches
        write_data_tap=reduce(or_,write_tap_branches)
        board=json.loads((Path(__file__).resolve().parents[3]/'config/board.json').read_text())
        pins={p['signal']:p for p in board['pins']}
        read_request=reduce(or_,[p.rddata_en for p in backend.phases])
        write_request=reduce(or_,[p.wrdata_en for p in backend.phases])
        rpipe=Signal(16);wpipe=Signal(16)
        self.sync += [rpipe.eq(Cat(read_request,rpipe)),wpipe.eq(Cat(write_request,wpipe))]
        if cl>=16:
            # Supported receive taps start at one. Keep every selectable tap
            # registered to remove the raw slow-serializer path at 250 MHz and above.
            effective_rd_delay=Mux(rd_delay_fast<1,1,rd_delay_fast)
            read_gate=Array([rpipe[i] for i in range(15)])[effective_rd_delay-1]
        else:
            read_gate=Array([read_request]+[rpipe[i] for i in range(15)])[rd_delay_fast]
        # Use only registered request history for supported write taps; a
        # direct request bypass creates a long slow-to-fast path into DQS OE.
        write_gate=Array([wpipe[i] for i in range(16)])[effective_wr_delay-1]
        wr_previous=Array([wpipe[i] for i in range(16)])[effective_wr_delay-2]
        wr_next=Array([wpipe[i] for i in range(16)])[effective_wr_delay]
        # Payload is qualified by per-phase capture validity. Reset clears
        # capture_mask; payload need not reset, shortening the IO-valid path.
        read_words=[Signal(64,reset_less=True,name=f'captured{g}') for g in range(4)]
        self.read_alignment=Signal(4)
        captured_word=Cat(*read_words)
        read_history=[Signal(256,reset_less=True,name=f'fullrate_read_history_{i}') for i in range(15)]
        self.sync += read_history[0].eq(captured_word)
        for i in range(1,15):self.sync += read_history[i].eq(read_history[i-1])
        aligned_word=Signal(256,reset_less=True,name='fullrate_aligned_return')
        aligned_word.attr.add('no_retiming')
        self.sync += aligned_word.eq(Array([captured_word]+read_history)[self.read_alignment])
        combined_valid=Cat(*[g.valid for g in self.groups])
        # Assemble fresh phase slices independently of the sticky diagnostic mask.
        # Completion is registered with read_words, then follows exactly the
        # same enabled-free history/alignment pipeline as captured_word.
        frame_mask=Signal(16)
        frame_valid=Signal()
        valid_history=[Signal(name=f'fullrate_read_valid_history_{i}') for i in range(15)]
        aligned_valid=Signal(name='fullrate_aligned_valid')
        aligned_valid.attr.add('no_retiming')
        self.sync += If(self.clear_capture,
            frame_mask.eq(0),frame_valid.eq(0),aligned_valid.eq(0),
            *[v.eq(0) for v in valid_history]
        ).Else(
            frame_valid.eq((frame_mask | combined_valid)==0xffff),
            If((frame_mask | combined_valid)==0xffff,frame_mask.eq(0))
            .Else(frame_mask.eq(frame_mask | combined_valid)),
            valid_history[0].eq(frame_valid),
            *[valid_history[i].eq(valid_history[i-1]) for i in range(1,15)],
            aligned_valid.eq(Array([frame_valid]+valid_history)[self.read_alignment])
        )
        self.read_first_age=Signal(16);self.read_last_age=Signal(16)
        read_age=Signal(16)
        seen_valid=Signal()
        diagnostic_read_request=read_request
        diagnostic_age_reset=0
        if cl>=16:
            # Sample the request before the wide diagnostic counter. At the
            # delayed reset edge the raw-request age is already one; seed
            # that value to preserve ages observed at later receive beats.
            diagnostic_read_request=Signal(name='diagnostic_read_request')
            diagnostic_read_request.attr.add('no_retiming')
            self.sync += diagnostic_read_request.eq(read_request)
            diagnostic_age_reset=1
        self.sync += If(diagnostic_read_request,read_age.eq(diagnostic_age_reset)).Else(
            If(read_age!=65535,read_age.eq(read_age+1)))
        diagnostic_valid=combined_valid!=0
        diagnostic_age=read_age
        diagnostic_seen_reset=diagnostic_read_request
        if pipelined_diagnostics:
            # Capture the 16 hard-PHY validity lanes locally, then register a
            # four-bit summary. This adds one event cycle versus the single
            # global-OR register used by V14. Keep age and reset aligned to
            # that same two-stage diagnostic pipeline. DFI/data capture below
            # continues to consume the original, un-delayed group.valid bits.
            self.submodules.diagnostic_valid_pipeline = PhyDiagnosticValidPipeline(
                [group.valid for group in self.groups])
            diagnostic_valid=self.diagnostic_valid_pipeline.valid
            diagnostic_age=Signal(16,name='diagnostic_age_registered_2',reset=0)
            diagnostic_age_first=Signal(16,name='diagnostic_age_registered_1',reset=0)
            diagnostic_seen_reset=Signal(name='diagnostic_seen_reset_registered_2',reset=0)
            diagnostic_seen_reset_first=Signal(name='diagnostic_seen_reset_registered_1',reset=0)
            for signal in [diagnostic_age,diagnostic_age_first,
                           diagnostic_seen_reset,diagnostic_seen_reset_first]:
                signal.attr.add('no_retiming')
            self.sync += [
                diagnostic_age_first.eq(read_age),
                diagnostic_age.eq(diagnostic_age_first),
                diagnostic_seen_reset_first.eq(diagnostic_read_request),
                diagnostic_seen_reset.eq(diagnostic_seen_reset_first),
            ]
        self.sync += If(diagnostic_seen_reset,seen_valid.eq(0))
        self.sync += If(diagnostic_valid,
            self.read_last_age.eq(diagnostic_age),
            If(~seen_valid,self.read_first_age.eq(diagnostic_age),seen_valid.eq(1)))
        self.sync += If(self.clear_capture,self.capture_mask.eq(0)).Else(self.capture_mask.eq(self.capture_mask|combined_valid))
        self.sync += If(diagnostic_valid,self.capture_count.eq(self.capture_count+1))
        for byte,group in enumerate(self.groups):
            members=sorted(range(byte*8,byte*8+8),key=lambda i:pins[f'io_ddr4_comp1_dq[{i}]']['io96']['io96_index'])
            packed=Cat(*[self.dfi.phases[sample//2].wrdata[32*(sample%2)+bit] for sample in range(8) for bit in members])
            self.comb += front_write_data[byte*64:(byte+1)*64].eq(packed)
            self.sync += [group.data_out.eq(write_data_tap[byte*64:(byte+1)*64]),group.oe.eq(Replicate(write_gate,4)),
                group.strobe.eq(Mux(write_gate,0x55,0)),
                group.strobe_oe.eq(Replicate(write_gate|wr_previous|wr_next,4)),
                group.read_en.eq(Mux(read_gate,read_mask_fast,0))]
            for sl in range(4):
                self.sync += If(group.valid[sl],read_words[byte][sl*16:(sl+1)*16].eq(group.data_in[sl*16:(sl+1)*16]))
            for sample in range(8):
                for pin,bit in enumerate(members):
                    self.comb += physical.phases[sample//2].rddata[32*(sample%2)+bit].eq(aligned_word[byte*64+sample*8+pin])
        for phase in physical.phases:
            self.comb += phase.rddata_valid.eq(aligned_valid)
        self.sync += If(wpipe[1] & reduce(or_,[p.wrdata_mask!=0 for p in backend.phases]),self.mask_error.eq(1))
        indices={4:[12,13,14,15,18,20,21,23],5:list(range(24,28))+list(range(30,36))+list(range(39,44))+list(range(45,48))}
        for g,locations in indices.items():
            words=[]
            for sample in range(8):
                phase=physical.phases[sample//2]
                for index in locations:
                    signal=next(p['signal'] for p in pins.values() if p['io96'] and p['io96']['io96_index']==index).replace('o_ddr4_comp1_','')
                    if signal.startswith('a['):
                        bit=int(signal[2:-1]); value=phase.address[bit]
                        if bit==14:value=phase.we_n
                        if bit==15:value=phase.cas_n
                        if bit==16:value=phase.ras_n
                    elif signal.startswith('ba['):value=phase.bank[int(signal[3:-1])]
                    elif signal.startswith('bg['):value=phase.bank[2+int(signal[3:-1])]
                    elif signal=='ck_p':value=Constant(sample%2,1)
                    elif signal=='ck_n':value=Constant(1-sample%2,1)
                    elif signal=='par':value=Constant(0,1) # MR5 parity disabled.
                    else:value=getattr(phase,signal)
                    words.append(value)
            self.sync += getattr(self,f'cmd{g}').eq(Cat(*words))

        # Each command group now uses the public single-ended strobe output.
        self.sync += [
            self.cmd4_strobe.eq(Cat(*[physical.phases[s//2].odt for s in range(8)])),
            self.cmd5_strobe.eq(Cat(*[physical.phases[s//2].address[4] for s in range(8)]))]
        # One command per fast frame is required: A5 is a fabric GPIO.
        valid=[(~p.cs_n[0]) & (~p.ras_n | ~p.cas_n | ~p.we_n) for p in backend.phases]
        selected=Signal()
        choice=Signal()
        conflict_valid=valid
        conflict_a5=[p.address[5] for p in physical.phases]
        if cl>=14:
            valid_registered=Signal(4,name='conflict_valid_registered')
            a5_registered=Signal(4,name='conflict_a5_registered')
            valid_registered.attr.add('no_retiming')
            a5_registered.attr.add('no_retiming')
            self.sync += [valid_registered.eq(Cat(*valid)),a5_registered.eq(Cat(*conflict_a5))]
            conflict_valid=[valid_registered[i] for i in range(4)]
            conflict_a5=[a5_registered[i] for i in range(4)]
        if cl>=16:
            # Select from fast-domain samples. Compensate this extra cycle
            # in the GPIO delay tap below, preserving supported pin timing.
            # Capture the active A5 terms and any-valid alongside the
            # existing phase samples, shortening choice to one six-input LUT.
            active_a5=Signal(4,name='a5_active_registered')
            any_valid=Signal(name='a5_any_valid_registered')
            active_a5.attr.add('no_retiming')
            any_valid.attr.add('no_retiming')
            self.sync += [active_a5.eq(Cat(*[valid[n]&physical.phases[n].address[5] for n in range(4)])),
                any_valid.eq(reduce(or_,valid))]
            self.comb += choice.eq(reduce(or_,[active_a5[n] for n in range(4)]) |
                (selected & ~any_valid))
        else:
            self.comb += choice.eq(selected)
            for n,p in enumerate(physical.phases):
                self.comb += If(valid[n],choice.eq(p.address[5]))
        conflict=reduce(or_,[conflict_valid[i]&conflict_valid[j]&(conflict_a5[i]!=conflict_a5[j]) for i in range(4) for j in range(i+1,4)])
        self.sync += [selected.eq(choice),If(conflict,self.command_conflict.eq(1))]
        gpio_pipe=[Signal(name=f'a5_gpio_pipe_{i}') for i in range(8)]
        for v in gpio_pipe:v.attr.add('no_retiming')
        self.sync += gpio_pipe[0].eq(choice)
        for i in range(1,len(gpio_pipe)):self.sync += gpio_pipe[i].eq(gpio_pipe[i-1])
        # This fast controller path now uses only registered GPIO history.
        # Minimum effective delay is two; CSR values 0 and 1 intentionally
        # alias tap2/pipe0, avoiding the unregistered `choice` bypass that
        # lengthened the IO-register setup path in V4. For taps 2..7, pipe
        # index (delay-2) is cycle-equivalent to V4's [choice]+pipe mux.
        gpio_tap_onehot = Signal(6, reset=1, name='a5_gpio_tap_onehot')
        gpio_tap_onehot.attr.add('no_retiming')
        tap_index = Mux(self.a5delay < 2, 0, self.a5delay - 2)
        tap_codes = Array([Constant(1 << i, 6) for i in range(6)])
        self.sync += gpio_tap_onehot.eq(tap_codes[tap_index])
        # Move one existing history cycle into three small selection
        # registers. Decode the raw CSR value at the same edge as the old
        # onehot register, then OR the registered branches into the IO FF.
        # This preserves all tap/control cycles, including changing delays.
        previous_words=[choice]+gpio_pipe[:5]
        branch=Signal(3,name='a5_selection_branch')
        branch.attr.add('preserve')
        branch.attr.add('no_retiming')
        selects=[self.a5delay <= 2]+[self.a5delay == i+2 for i in range(1,6)]
        for i in range(3):
            self.sync += branch[i].eq((previous_words[2*i] & selects[2*i]) |
                (previous_words[2*i+1] & selects[2*i+1]))
        # Replicate the synchronous reset into a local one-load register.
        # The IO output has its own explicit reset mux, one cycle delayed.
        # A5 is don't-care during reset (the PHY/reset initialization holds
        # command traffic inactive); active command timing stays unchanged.
        local_reset=Signal(reset=1,name='a5_local_reset')
        local_reset.attr.add('preserve')
        local_reset.attr.add('no_retiming')
        self.sync += local_reset.eq(0)
        self.sync += self.a5gpio.eq(Mux(local_reset, 0, branch != 0))
