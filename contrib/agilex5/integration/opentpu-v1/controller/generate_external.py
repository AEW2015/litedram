#!/usr/bin/env python3
"""Opt-in V231 source transformation; never modifies the preserved baseline."""
import argparse, hashlib, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT)]+[str(ROOT/'third_party'/n) for n in ('migen','litex','litedram')]

def transformed():
    path=ROOT/'source/generator_seed.py'
    s=path.read_text().replace("root=Path(__file__).resolve().parents[1]",f"root=Path({str(ROOT)!r})")
    s=s.replace('        control=Signal(4);', "        self.c_request=Signal(name='c_request'); self.c_granted=Signal(name='c_granted')\n        control=Signal(4);")
    s=s.replace('        real_port=port','''        physical_port=port
        real_port=LiteDRAMNativePort('both',physical_port.address_width,256)
        self.c_ready=Signal(name='c_ready'); self.c_drain_req=Signal(name='c_drain_req')
        self.c_drain_done=Signal(name='c_drain_done')
''')
    s=s.replace('FullBurstWriteMaskGuard(real_port)', 'FullBurstWriteMaskGuard(physical_port)')
    write_start=s.index('        write=lambda n:')
    write_end=s.index('\n',write_start)
    original=s[write_start:write_end].replace('        write=lambda n:', '        unowned_write=lambda n:')
    s=s[:write_start]+original+\
        '\n        write=lambda n:unowned_write(n) & ~self.c_request & ~self.c_granted'+s[write_end:]
    marker='        native_command=port.cmd'
    block='''        # The external owner cannot change while native requests are in flight.
        ext=Record([('cmd_valid',1),('cmd_ready',1),('cmd_we',1),('cmd_address',28),
            ('wdata_valid',1),('wdata_ready',1),('wdata_data',256),('wdata_we',32),
            ('rdata_valid',1),('rdata_ready',1),('rdata_data',256)],name='c')
        self.c=ext
        quiet=Signal(7)
        ingress_empty=~self.dma_command_input.source.valid & ~self.dma_command_buffer.nonempty
        activity=(physical_port.cmd.valid & physical_port.cmd.ready) | (physical_port.wdata.valid & physical_port.wdata.ready) | physical_port.rdata.valid
        self.sync += If(~striped.drained | ~ingress_empty | activity,quiet.eq(0)).Elif(quiet!=64,quiet.eq(quiet+1))
        self.comb += [self.c_ready.eq(control[3] & phy.locked),
            self.c_drain_done.eq(self.c_granted & striped.drained & ingress_empty & (quiet==64) & ~ext.cmd_valid & ~ext.wdata_valid)]
        self.sync += If(~self.c_granted,
            If(self.c_request & self.c_ready & ~busy & ~dma.busy & ~dma.start & ingress_empty & striped.drained & (quiet==64),self.c_granted.eq(1))
        ).Elif(~self.c_request & self.c_drain_done,self.c_granted.eq(0))
        self.comb += [
            physical_port.cmd.valid.eq(Mux(self.c_granted,ext.cmd_valid & ~self.c_drain_req,real_port.cmd.valid)),
            physical_port.cmd.we.eq(Mux(self.c_granted,ext.cmd_we,real_port.cmd.we)),
            physical_port.cmd.addr.eq(Mux(self.c_granted,ext.cmd_address,real_port.cmd.addr)),
            physical_port.cmd.last.eq(Mux(self.c_granted,1,real_port.cmd.last)),
            real_port.cmd.ready.eq(physical_port.cmd.ready & ~self.c_granted),
            ext.cmd_ready.eq(physical_port.cmd.ready & self.c_granted & ~self.c_drain_req),
            physical_port.wdata.valid.eq(Mux(self.c_granted,ext.wdata_valid,real_port.wdata.valid)),
            physical_port.wdata.data.eq(Mux(self.c_granted,ext.wdata_data,real_port.wdata.data)),
            physical_port.wdata.we.eq(Mux(self.c_granted,ext.wdata_we,real_port.wdata.we)),
            real_port.wdata.ready.eq(physical_port.wdata.ready & ~self.c_granted),
            ext.wdata_ready.eq(physical_port.wdata.ready & self.c_granted),
            physical_port.rdata.ready.eq(Mux(self.c_granted,ext.rdata_ready,real_port.rdata.ready)),
            real_port.rdata.valid.eq(physical_port.rdata.valid & ~self.c_granted),
            real_port.rdata.data.eq(physical_port.rdata.data),
            ext.rdata_valid.eq(physical_port.rdata.valid & self.c_granted),
            ext.rdata_data.eq(physical_port.rdata.data)]
'''
    assert s.count(marker)==1
    s=s.replace(marker,block+marker)
    s=s.replace('start_write.eq(write(7)&self.bus.writedata[0])','start_write.eq(write(7)&self.bus.writedata[0] & ~self.c_request & ~self.c_granted)')
    s=s.replace('start_read.eq(write(7)&self.bus.writedata[1])','start_read.eq(write(7)&self.bus.writedata[1] & ~self.c_request & ~self.c_granted)')
    s=s.replace('~busy & control[3] & locked_slow,self.csr_request_pipeline','~busy & control[3] & locked_slow & ~self.c_request & ~self.c_granted,self.csr_request_pipeline')
    s=s.replace('        for group in phy.groups:self.ios.update(group.flatten())',"        self.ios.update(set(ext.flatten()) | {self.c_request,self.c_granted,self.c_ready,self.c_drain_req,self.c_drain_done})\n        for group in phy.groups:self.ios.update(group.flatten())")
    # Initialization/PHY CSR writes are a software ownership contract: forbidden
    # while an accelerator request is active. Native diagnostic starts are blocked.
    return s,path

def make_controller():
    s,path=transformed(); ns={'__file__':str(path)}; exec(compile(s,str(path),'exec'),ns)
    return ns['Diagnostic2933Controller'](),s,path

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args()
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    if (out/'litedram_external_controller.v').exists(): raise SystemExit('refusing to overwrite generated controller')
    dut,s,path=make_controller()
    from migen.fhdl import verilog
    (out/'litedram_external_controller.v').write_text(str(verilog.convert(dut,ios=dut.ios,name='litedram_external_controller')))
    (out/'external_generator_snapshot.py').write_text(s)
    (out/'controller_external_manifest.json').write_text(json.dumps({'baseline_generator':str(path.relative_to(ROOT)),'baseline_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'source_sha256':hashlib.sha256(s.encode()).hexdigest(),'timing_inherited':False,'native_width':256,'address_width':28,'drain_quiet_cycles':64},indent=2)+'\n')
