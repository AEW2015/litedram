#!/usr/bin/env python3
"""Isolated DDR2666 profile: regenerated native timing and CL20/CWL14."""
import argparse,hashlib,json,re,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'controller'))
from generate_external import transformed as external_transformed
sys.path.insert(0,str(HERE))

def transformed():
    source,baseline=external_transformed()
    old='from litedram_phy.fullrate_dma_engine_v46 import MemoryDMA'
    assert source.count(old)==1
    source=source.replace(old,'from fullrate_dma_v5 import MemoryDMA')
    marker='        physical_port=port\n'
    assert source.count(marker)==1
    source=source.replace(marker,'''        from litex.soc.interconnect import stream
        striped_port=port
        physical_port=LiteDRAMNativePort('both',striped_port.address_width,256)
        # A single elastic write-data stage isolates the diagnostic/ownership
        # payload muxes from the shallow physical stripe FIFO RAM inputs.
        self.submodules.write_ingress=write_ingress=stream.Buffer(
            physical_port.wdata.description,pipe_valid=True,pipe_ready=False)
        self.comb += [physical_port.cmd.connect(striped_port.cmd),
            physical_port.wdata.connect(write_ingress.sink),
            write_ingress.source.connect(striped_port.wdata),
            striped_port.rdata.connect(physical_port.rdata)]
        for signal in write_ingress.pipe_valid.source.flatten():
            signal.attr.add('preserve');signal.attr.add('no_retiming')
''')
    # An ingress stage is an outstanding transaction until its payload reaches
    # the physical stripe. Include it everywhere ownership/diagnostics drain.
    source=source.replace('~self.dma_command_buffer.nonempty',
        '~self.dma_command_buffer.nonempty & ~write_ingress.source.valid')
    marker='        activity=(physical_port.cmd.valid & physical_port.cmd.ready) | (physical_port.wdata.valid & physical_port.wdata.ready) | physical_port.rdata.valid'
    assert source.count(marker)==1
    source=source.replace(marker,marker+' | (striped_port.wdata.valid & striped_port.wdata.ready) | (csr_data_pipe.accepted_write & ~self.c_request & ~grant_csr)')
    source=source.replace('self.c_request & self.c_ready & ~busy & ~dma.busy & ~dma.start & ingress_empty',
        'self.c_request & self.c_ready & ~self.csr_request_pipeline.busy & ~csr_data_pipe.pending & ~busy & ~dma.busy & ~dma.start & ingress_empty')
    # Preserve identical state transitions on distinct local grant registers.
    # Payload/CSR ownership changes on the original edge; no additional grant
    # latency or interval where ingress can switch to the other client.
    marker='        self.c=ext\n'
    assert source.count(marker)==1
    source=source.replace(marker,marker+'''        grant_cmd=Signal(name='grant_cmd');grant_write=Signal(name='grant_write')
        grant_read=Signal(name='grant_read');grant_csr=Signal(name='grant_csr')
        grants=[self.c_granted,grant_cmd,grant_write,grant_read,grant_csr]
        for signal in grants:
            signal.attr.add('preserve');signal.attr.add('no_retiming')
''')
    source=source.replace('self.c_granted.eq(1))','*[signal.eq(1) for signal in grants])')
    source=source.replace('self.c_granted.eq(0))','*[signal.eq(0) for signal in grants])')
    # CSR lambda precedes this block. Define its local grant replica before
    # the lambda references it; remove only the later duplicate declaration.
    source=source.replace("grant_read=Signal(name='grant_read');grant_csr=Signal(name='grant_csr')", "grant_read=Signal(name='grant_read')")
    source=source.replace("        self.c_request=Signal(name='c_request'); self.c_granted=Signal(name='c_granted')", "        self.c_request=Signal(name='c_request'); self.c_granted=Signal(name='c_granted')\n        grant_csr=Signal(name='grant_csr')")
    source=source.replace('write=lambda n:unowned_write(n) & ~self.c_request & ~self.c_granted',
        'write=lambda n:unowned_write(n) & ~self.c_request & ~grant_csr')
    # Only the mux/ready cones get local replicas; primary grant remains the
    # authoritative interface status and drives ownership transition logic.
    lines=source.splitlines()
    for i,line in enumerate(lines):
        if 'physical_port.cmd.' in line or 'real_port.cmd.ready.eq' in line or 'ext.cmd_ready.eq' in line:
            if '.eq(' in line:lines[i]=line.replace('self.c_granted','grant_cmd')
        if 'physical_port.wdata.' in line or 'real_port.wdata.ready.eq' in line or 'ext.wdata_ready.eq' in line:
            if '.eq(' in line:lines[i]=line.replace('self.c_granted','grant_write')
        if 'physical_port.rdata.' in line or 'real_port.rdata.valid.eq' in line or 'ext.rdata_valid.eq' in line:
            if '.eq(' in line:lines[i]=line.replace('self.c_granted','grant_read')
    source='\n'.join(lines)+'\n'
    # The original separate diagnostic DMA drain expression must cover the
    # newly introduced pending write stage as well.
    source=source.replace('dma.drained.eq(striped.drained',
        'dma.drained.eq(~write_ingress.source.valid & striped.drained')
    source=source.replace('84:striped.drained}', '84:(striped.drained & ~write_ingress.source.valid)}')
    # Physical PLL profile is M5/N1 with CK/4 controller timing. The preserved
    # V22 command pipeline adds2 cycles to the explicit22-cycle return offset.
    changes={'controller_clock_hz=1600000000.0/4':'controller_clock_hz=(4000000000.0/3)/4',
             'cl,cwl=22,16':'cl,cwl=20,14',
             'tWR=24/(4400000000/3)*1e9':'tWR=24/(4000000000/3)*1e9',
             'memory_clock_mhz=1467':'memory_clock_mhz=1333',
             '4400000000/3':'4000000000/3'}
    for old,new in changes.items():
        assert source.count(old)==1,old
        source=source.replace(old,new)
    source=source.replace("memory_clock_mhz == 1467","memory_clock_mhz == 1333").replace("memory_clock_mhz==1467","memory_clock_mhz==1333")
    marker='        try:\n            dma_make_core(self,phy,memory,controller_clock_hz,settings)'
    assert source.count(marker)==1
    source=source.replace(marker,"""        if not registered_refresh_timer:
            from segmented_timer_v8 import SegmentedRefreshTimer
            # Preserve the ordinary refresh timer; only the one-second wide
            # ZQCS interval uses registered prefix-borrow segmentation.
            vendor_refresher.RefreshTimer=lambda cycles: (SegmentedRefreshTimer(cycles)
                if cycles>=(1<<20) else previous_timer(cycles))
"""+marker)
    # Original safe eligibility is sampled while request already blocks all
    # diagnostic starts/writes. Final local grant condition rechecks current
    # request, calibration, pending CSR transactions and physical activity.
    original_eligible='self.c_request & self.c_ready & ~self.csr_request_pipeline.busy & ~csr_data_pipe.pending & ~busy & ~dma.busy & ~dma.start & ingress_empty & striped.drained & (quiet==64)'
    assert source.count(original_eligible)==1
    marker='        self.sync += If(~self.c_granted,\n'
    assert source.count(marker)==1
    source=source.replace(marker,"""        self.external_acquire_current_ok=Signal(name='external_acquire_current_ok')
        self.external_acquire_safe=Signal(name='external_acquire_safe')
        self.external_acquire_safe.attr.add('preserve')
        self.external_acquire_safe.attr.add('dont_merge')
        self.external_acquire_safe.attr.add('no_retiming')
        self.comb += self.external_acquire_current_ok.eq(ACQUIRE_ELIGIBLE)
        self.sync += self.external_acquire_safe.eq(~self.c_granted & self.external_acquire_current_ok & ~activity)
""".replace('ACQUIRE_ELIGIBLE',original_eligible)+marker)
    source=source.replace(original_eligible+',*[signal.eq(1)',
        'self.external_acquire_safe & self.c_request & self.c_ready & ~self.csr_request_pipeline.busy & ~csr_data_pipe.pending & ~activity,*[signal.eq(1)')
    return source,baseline

def make_controller():
    source,baseline=transformed();ns={'__file__':str(baseline)}
    exec(compile(source,str(baseline),'exec'),ns)
    return ns['Diagnostic2933Controller'](),source,baseline

def generate(output):
    output.mkdir(parents=True,exist_ok=True)
    if (output/'litedram_external_controller.v').exists():raise SystemExit('refusing overwrite')
    from migen.fhdl import verilog
    dut,source,baseline=make_controller()
    rtl=str(verilog.convert(dut,ios=dut.ios,name='litedram_external_controller'))
    arrays=re.findall(r'^reg \[\d+:0\] (storage(?:_\d+)?)\[0:(\d+)\];$',rtl,re.M)
    assert len(arrays)==35 and all(int(depth)<128 for _,depth in arrays)
    rtl,n=re.subn(r'^(reg \[\d+:0\] storage(?:_\d+)?\[0:\d+\];)$',
        lambda m:'(* ramstyle = "MLAB, no_rw_check" *) '+m.group(1),rtl,flags=re.M)
    assert n==35 and not any(ord(c)<32 and c not in '\n\r\t' for c in rtl)
    (output/'litedram_external_controller.v').write_text(rtl)
    (output/'external_generator_snapshot.py').write_text(source)
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    manifest={'baseline_generator':str(baseline),'baseline_sha256':sha(baseline),
      'external_generator_sha256':sha(HERE.parent/'controller/generate_external.py'),
      'dma_revision_sha256':sha(HERE/'fullrate_dma_v5.py'),'generator_sha256':sha(Path(__file__)),
      'generated_rtl_sha256':sha(output/'litedram_external_controller.v'),
      'fifo_arrays':arrays,'native_write_ingress_slots':1,'drain_includes_ingress':True,
      'ownership_replicas':4,'timing_inherited':False,'requires_independent_fit':True}
    (output/'controller_external_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',default=str(HERE))
    generate(Path(parser.parse_args().output))
