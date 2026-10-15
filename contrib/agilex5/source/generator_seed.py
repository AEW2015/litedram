#!/usr/bin/env python3
"""Generate the soft DDR4 controller and full-burst diagnostic interface."""
from pathlib import Path
import sys,json
import math
root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root)]+[str(root/'third_party'/n) for n in ['migen','litex','litedram']]
from migen import *
from migen.fhdl import verilog
from litedram.modules import MT40A2G8, _SpeedgradeTimings
from litedram.core import LiteDRAMCore
from litedram.core.controller import ControllerSettings
from litedram.phy.dfi import Interface, DFIRateConverter
from litedram_phy.agilex5_fullrate_v22 import Agilex5FullRatePHYV22 as Agilex5Primitive2933PHY

class Board2933Memory(MT40A2G8):
    # Same 16Gb x8 die geometry; conservative 2933 timing, not a blind
    # frequency change to the previous 2666 module table.
    speedgrade_timings = {"2933": _SpeedgradeTimings(
        tRP=20, tRCD=20, tWR=24/(4400000000/3)*1e9,
        tRFC={"1x": (None, 350), "2x": (None, 260), "4x": (None, 160)},
        tFAW=(20, 25), tRAS=32)}
    speedgrade_timings["default"] = speedgrade_timings["2933"]

class Diagnostic2933Controller(Module):
    def __init__(self, memory_clock_mhz=1467, memory_clock_hz=None, registered_row_hit=True, registered_refresh_timer=False):
        assert memory_clock_mhz == 1467
        # Isolated CL22/CWL16 profile; all counters use the actual rational CK.
        if memory_clock_hz is not None:
            assert memory_clock_mhz == 1467
            assert math.isclose(memory_clock_hz, 4400000000/3, rel_tol=1e-12)
        controller_clock_hz=1600000000.0/4
        attrs=['read_en_mask','groups','cmd4','cmd5','locked','rd_delay','wr_delay','clear_capture','capture_mask','capture_count','mask_error','cmd4_strobe','cmd5_strobe','a5gpio','a5delay','command_conflict','read_first_age','read_last_age']
        # slow_clk rises just after a sys2x edge. Initialize the serializer
        # counters at ratio-1 so that first-edge advancement selects half 0.
        WrappedPHY=Agilex5Primitive2933PHY
        cl,cwl=22,16
        self.submodules.phy=phy=WrappedPHY(cl=cl,cwl=cwl,pipelined_diagnostics=True,read_latency=22)

        # Geometry matches the x8 die organization in the working EMIF profile.
        memory=Board2933Memory(controller_clock_hz,'1:4',speedgrade='2933')
        # Conservative profile no faster than board's confirmed timing values.
        memory.timing_settings.tRP=math.ceil(20e-9*controller_clock_hz)
        memory.timing_settings.tRCD=memory.timing_settings.tRP
        if memory_clock_mhz>=800:
            # tREFI is a maximum interval; do not apply the model's rounding
            # margin for minimum delays to the board's 7.8 us refresh period.
            memory.timing_settings.tREFI=math.floor(7.8e-6*controller_clock_hz)
        if memory_clock_mhz==1467:
            # Keep the activate-to-activate setting consistent with the
            # conservative programmed row-active and precharge intervals.
            memory.timing_settings.tRC=max(memory.timing_settings.tRC,
                memory.timing_settings.tRAS+memory.timing_settings.tRP)
        memory.timing_settings.tWR=max(memory.timing_settings.tWR,math.ceil(24/4))
        memory.timing_settings.tCCD=max(memory.timing_settings.tCCD,math.ceil(8/4))
        self.memory_timing=memory.timing_settings
        settings=ControllerSettings(cmd_buffer_depth=1,cmd_buffer_buffered=True,with_auto_precharge=False)
        settings.registered_row_hit=registered_row_hit
        # Local generation option: retain vendor timing/handshakes while
        # predicting timer expiration into a register on the same cycle.
        from litedram.core import refresher as vendor_refresher
        previous_timer=vendor_refresher.RefreshTimer
        if registered_refresh_timer:
            from litedram_phy.refresh_3200_v2 import RefreshTimer2933
            vendor_refresher.RefreshTimer=RefreshTimer2933
        try:
            dma_make_core(self,phy,memory,controller_clock_hz,settings)
            dram=self.dram
        finally:
            vendor_refresher.RefreshTimer=previous_timer
        mux=dram.controller.multiplexer
        self.external_bus=Record([('address',12),('read',1),('write',1),('writedata',32),('readdata',32),('readdatavalid',1),('waitrequest',1)],name='csr')
        self.bus=Record(self.external_bus.layout,name='internal_csr')
        from litedram_phy.fullrate_csr_request_pipeline_v6 import FullRateCSRRequestPipeline
        self.submodules.csr_request_pipeline=FullRateCSRRequestPipeline(self.external_bus,self.bus)
        control=Signal(4); cmd=Signal(6); address=Signal(17);bank=Signal(4); phase_index=Signal(3,reset=3)
        burst_address=Signal(28);write_data=Signal(256);read_data=Signal(256)
        busy=Signal();done=Signal();error=Signal();last_valid=Signal(16);timeout=Signal(16)
        command_issue=Signal();start_write=Signal();start_read=Signal()
        adr=self.bus.address[2:]
        from litedram_phy.fullrate_csr_write_v2 import ManualWriteDataPipeline
        self.submodules.csr_write_data_pipeline=csr_data_pipe=ManualWriteDataPipeline(self.bus,self.csr_request_pipeline.address_select)
        write=lambda n:(self.csr_request_pipeline.phy_write_request(n)) if n in (25,26,40,41,78) else csr_data_pipe.accepted_write & self.csr_request_pipeline.address_select[n]
        self.comb += [command_issue.eq(write(5)),start_write.eq(write(7)&self.bus.writedata[0]),start_read.eq(write(7)&self.bus.writedata[1]),dram.dfii.ext_dfi_sel.eq(~control[3])]
        # Hold manual command class before/after the one-phase CS pulse.
        # DDR4 ignores CA while CS is high; this lets MPR diagnostics isolate
        # inter-lane command setup from the data receive path.
        inject=dram.dfii.ext_dfi
        for n,p in enumerate(inject.phases):
            issue=command_issue & (phase_index==n)
            self.comb += [p.address.eq(address),p.bank.eq(bank),p.cs_n.eq(~(issue&cmd[0])),p.we_n.eq(~cmd[1]),p.cas_n.eq(~cmd[2]),p.ras_n.eq(~cmd[3]),p.cke.eq(control[1]),p.odt.eq(control[2]),p.reset_n.eq(control[0]),p.wrdata_en.eq(issue&cmd[4]),p.rddata_en.eq(issue&cmd[5]),p.wrdata.eq(write_data[n*len(p.wrdata):(n+1)*len(p.wrdata)]),p.wrdata_mask.eq(0)]
        self.sync += [If(write(1),control.eq(self.bus.writedata)),If(write(2),cmd.eq(self.bus.writedata)),If(write(3),address.eq(self.bus.writedata)),If(write(4),bank.eq(self.bus.writedata)),If(write(6),burst_address.eq(self.bus.writedata)),If(write(24),phase_index.eq(self.bus.writedata)),If(write(25),phy.rd_delay.eq(self.bus.writedata)),If(write(26),phy.wr_delay.eq(self.bus.writedata)),If(write(40),phy.read_en_mask.eq(self.bus.writedata)),If(write(41),phy.a5delay.eq(self.bus.writedata))]
        for i in range(8):self.sync += If(csr_data_pipe.enable[i],write_data[i*32:(i+1)*32].eq(csr_data_pipe.data))
        from litedram_phy.fullrate_striped_v35 import FullRateStripedPort as StripedPort
        children=[dram.crossbar.get_port(mode='both',data_width=256) for _ in range(4)]
        self.submodules.striped=striped=StripedPort(children)
        port=striped.port
        from litedram.common import LiteDRAMNativePort
        from litedram_phy.fullrate_dma_engine_v46 import MemoryDMA
        real_port=port
        from litedram_phy.fullrate_write_mask_guard_v1 import FullBurstWriteMaskGuard
        self.submodules.mask_guard=mask_guard=FullBurstWriteMaskGuard(real_port)
        port=LiteDRAMNativePort('both', real_port.address_width, 256)
        self.submodules.dma=dma=MemoryDMA(real_port.address_width,fifo_depth=64)
        from litex.soc.interconnect import stream
        from litedram_phy.fullrate_striped_v35 import OutputRegisteredFIFO
        self.submodules.dma_command_buffer=OutputRegisteredFIFO(real_port.cmd.description,depth=4,buffered=True)
        self.comb += self.dma_command_buffer.source.connect(real_port.cmd)
        self.submodules.dma_command_input=stream.Buffer(real_port.cmd.description,pipe_valid=True,pipe_ready=True)
        self.comb += self.dma_command_input.source.connect(self.dma_command_buffer.sink)
        for ep in ['cmd','wdata']:
            target=self.dma_command_input.sink if ep=='cmd' else getattr(real_port,ep); diag=getattr(port,ep); run=getattr(dma.port,ep)
            owner_select=dma.command_select if ep=='cmd' else dma.write_select
            self.comb += [target.valid.eq(Mux(owner_select,run.valid,diag.valid)), diag.ready.eq(target.ready & ~owner_select), run.ready.eq(target.ready & owner_select)]
            for field,_ in target.description.payload_layout:
                self.comb += getattr(target,field).eq(Mux(owner_select,getattr(run,field),getattr(diag,field)))
        self.comb += [real_port.rdata.ready.eq(Mux(dma.read_select,dma.port.rdata.ready,port.rdata.ready)), port.rdata.valid.eq(real_port.rdata.valid & ~dma.read_select), dma.port.rdata.valid.eq(real_port.rdata.valid & dma.read_select), port.rdata.data.eq(real_port.rdata.data), dma.port.rdata.data.eq(real_port.rdata.data)]
        native_command=port.cmd
        if memory_clock_mhz==1467:
            # Register the frontend ready path without moving bank-machine
            # command acceptance or its DRAM timing counters. Write data and
            # read completion still handshake directly with the crossbar.
            from litex.soc.interconnect import stream
            self.submodules.native_command_buffer=stream.Buffer(
                port.cmd.description,pipe_valid=True,pipe_ready=True)
            self.comb += self.native_command_buffer.source.connect(port.cmd)
            native_command=self.native_command_buffer.sink
        self.submodules.fsm=fsm=FSM(reset_state='IDLE')
        clear_capture=command_issue & cmd[5] | start_read
        if memory_clock_mhz>=900:
            # Capture data arrives many PHY clocks after the command. Register
            # this diagnostic-clear pulse in the slow domain so CSR address
            # decoding does not sit on a half-cycle path into the fast mask.
            clear_capture_registered=Signal(name='clear_capture_registered')
            clear_capture_registered.attr.add('no_retiming')
            self.sync += clear_capture_registered.eq(clear_capture)
            clear_capture=clear_capture_registered
        self.comb += [native_command.addr.eq(burst_address),native_command.last.eq(1),port.wdata.data.eq(write_data),port.wdata.we.eq(0xffffffff),port.rdata.ready.eq(1),phy.clear_capture.eq(clear_capture)]
        fsm.act('IDLE',
            If(start_write|start_read,NextValue(done,0),NextValue(error,0),NextValue(timeout,0),
                If(control[3]&phy.locked,NextValue(busy,1),If(start_write,NextState('W_CMD')).Else(NextState('R_CMD'))).Else(NextValue(error,1),NextValue(done,1))))
        for state,is_write in [('W_CMD',1),('R_CMD',0)]:
            fsm.act(state,native_command.valid.eq(1),native_command.we.eq(is_write),If(native_command.ready,NextState('WRITE' if is_write else 'READ')))
        fsm.act('WRITE',port.wdata.valid.eq(1),If(port.wdata.ready,NextValue(busy,0),NextValue(done,1),NextState('IDLE')))
        fsm.act('READ',If(port.rdata.valid,NextValue(read_data,port.rdata.data),NextValue(last_valid,phy.capture_mask),NextValue(error,phy.capture_mask!=0xffff),NextValue(busy,0),NextValue(done,1),NextState('IDLE')))
        # The frontend never reports success solely from fixed controller latency.
        self.sync += If(busy,timeout.eq(timeout+1),If(timeout==0xfffe,error.eq(1),done.eq(1)))
        # Sample related fast-domain diagnostics before the wide CSR mux.
        # Captures remain stable between bursts; firmware reads after completion.
        capture_slow=Signal(16);count_slow=Signal(32);mask_error_slow=Signal()
        locked_slow=Signal();raw_slow=Signal(256)
        raw_capture=Cat(*[p.rddata for p in phy.dfi.phases])
        self.sync += [capture_slow.eq(phy.capture_mask),count_slow.eq(phy.capture_count),
            mask_error_slow.eq(phy.mask_error | mask_guard.error),locked_slow.eq(phy.locked),raw_slow.eq(raw_capture)]
        values={0:Cat(locked_slow,busy,done,error,mask_error_slow),1:control,2:cmd,3:address,4:bank,6:burst_address,24:phase_index,25:phy.rd_delay,26:phy.wr_delay,27:last_valid,28:capture_slow,29:count_slow,40:phy.read_en_mask,41:phy.a5delay,42:phy.command_conflict}
        values.update({43:phy.read_first_age,44:phy.read_last_age})
        for i in range(8):values[8+i]=write_data[i*32:(i+1)*32];values[16+i]=read_data[i*32:(i+1)*32]
        for n in range(8):values[32+n]=raw_slow[n*32:(n+1)*32]
        dma.add_csrs(self, self.bus, adr, values, ~busy & control[3] & locked_slow,self.csr_request_pipeline.address_select)
        values[78]=phy.read_alignment
        self.sync += If(write(78),phy.read_alignment.eq(self.bus.writedata))
        from litedram_phy.fullrate_event_counter_v2 import PipelinedEventCounter
        self.comb += mux.choose_cmd.monitor_clear.eq(dma.start)
        diag_events=[mux.choose_cmd.accepted_read_q != 0,
            mux.choose_cmd.accepted_write_q != 0,
            mux.choose_cmd.accepted_command_q != 0,
            dram.controller.refresher.cmd.valid]
        diag_counters=[]
        for event in diag_events:
            counter=PipelinedEventCounter()
            self.submodules += counter
            self.comb += [counter.clear.eq(dma.start),counter.event.eq(event)]
            diag_counters.append(counter.value)
        cas_read_count,cas_write_count,bank_command_count,refresh_count=diag_counters
        self.comb += dma.drained.eq(striped.drained)
        values.update({79:cas_read_count,80:cas_write_count,81:bank_command_count,82:refresh_count,83:striped.error,84:striped.drained})
        read_mux=Signal(32)
        from litedram_phy.fullrate_csr_read_mux_v1 import FullRateCSRReadMux
        self.submodules.csr_read_mux=csr_read_mux=FullRateCSRReadMux(self.csr_request_pipeline.address_select,values)
        self.comb += read_mux.eq(csr_read_mux.data)
        self.sync += [self.bus.readdatavalid.eq(csr_data_pipe.accepted_read),If(self.bus.read,self.bus.readdata.eq(read_mux))]
        self.ios=set(self.external_bus.flatten())|{phy.locked,phy.cmd4,phy.cmd5,phy.cmd4_strobe,phy.cmd5_strobe,phy.a5gpio}
        for group in phy.groups:self.ios.update(group.flatten())



def dma_make_core(owner,phy,memory,hz,settings):
    import litedram.core as core_module
    from litedram.core import controller as controller_module
    from litedram_phy.fullrate_bankmachine_v44 import BankMachine
    from litedram_phy.fullrate_crossbar_v4 import LiteDRAMCrossbar
    from litedram_phy.fullrate_multiplexer_unified_v32 import Multiplexer
    import litedram_phy.fullrate_multiplexer_unified_v32 as mux_module
    from litedram_phy.fullrate_slot_chooser_v23 import SlotCommandChooserV23 as SlotCommandChooser
    previous_chooser=mux_module._CommandChooser
    mux_module._CommandChooser=SlotCommandChooser
    settings.with_agilex_fullrate_bankgroups=True
    settings.with_agilex_static_bg_ownership=True
    settings.cmd_buffer_depth=4
    previous=(controller_module.BankMachine,controller_module.Multiplexer,core_module.LiteDRAMCrossbar)
    controller_module.BankMachine=BankMachine
    controller_module.Multiplexer=Multiplexer
    core_module.LiteDRAMCrossbar=LiteDRAMCrossbar
    try:
        owner.submodules.dram=LiteDRAMCore(phy,memory.geom_settings,memory.timing_settings,hz,controller_settings=settings)
    finally:
        controller_module.BankMachine,controller_module.Multiplexer,core_module.LiteDRAMCrossbar=previous
        mux_module._CommandChooser=previous_chooser
