#!/usr/bin/env python3
"""Isolated native200 DDR1600 controller, single-rank TwinDie x32 8GiB per bank."""
from pathlib import Path
import sys,importlib.util,re,json,hashlib,argparse
R=Path(__file__).resolve().parents[3];HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE));V8=R/'integration/opentpu-v1/controller-v8-ddr2666';sys.path.insert(0,str(V8));spec=importlib.util.spec_from_file_location('native1600_parent',V8/'generate_external_v8_ddr2666.py');parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
def transformed(base_read_latency=20):
 s,b=parent.transformed()
 old='from litedram_phy.agilex5_fullrate_v22 import Agilex5FullRatePHYV22 as Agilex5Primitive2933PHY';assert s.count(old)==1;s=s.replace(old,'from phy_native1600_v1 import Agilex5Native1600PHY as Agilex5Primitive2933PHY')
 s=s.replace('from litedram.modules import MT40A2G8, _SpeedgradeTimings','from litedram.modules import MT40A2G8, MT40A2G16, _SpeedgradeTimings, _TechnologyTimings')
 s=s.replace('class Board2933Memory(MT40A2G8):','class Board2933Memory(MT40A2G16):')
 a=s.index('    speedgrade_timings =');z=s.index('\nclass Diagnostic2933Controller',a)
 s=s[:a]+'''    # Conservative design bounds; normal 1x REF commands at hot-case 3.90625us.
    technology_timings = _TechnologyTimings(
        tREFI={"1x":3906.25,"2x":1953.125,"4x":976.5625},
        tWTR=(8,10),tCCD=(5,None),tRRD=(8,10),tZQCS=(128,160))
    speedgrade_timings={"2933":_SpeedgradeTimings(
        tRP=20,tRCD=20,tWR=15,tRFC={"1x":(None,600),"2x":(None,600),"4x":(None,600)},
        tFAW=(32,40),tRAS=40)}
    speedgrade_timings["default"]=speedgrade_timings["2933"]
'''+s[z:]
 s=s.replace('memory_clock_mhz=1333','memory_clock_mhz=800').replace('memory_clock_mhz == 1333','memory_clock_mhz == 800').replace('memory_clock_mhz==1333','memory_clock_mhz==800').replace('4000000000/3','800000000').replace('4000000000.0/3','800000000.0')
 s=s.replace('cl,cwl=20,14','cl,cwl=12,11').replace('read_latency=22',f'read_latency={int(base_read_latency)}').replace('math.ceil(24/4)','math.ceil(12/4)').replace('math.ceil(8/4)','math.ceil(5/4)').replace('math.floor(7.8e-6*controller_clock_hz)','math.floor(3.90625e-6*controller_clock_hz)')
 s=s.replace('Board2933Memory','BoardNative1600Memory').replace('Diagnostic2933Controller','DiagnosticNative1600Controller').replace("speedgrade='2933'","speedgrade='1600'").replace('"2933"','"1600"')
 return s,b

def make_controller(base_read_latency=20):
 s,b=transformed(base_read_latency);ns={'__file__':str(b)};exec(compile(s,str(b),'exec'),ns);return ns['DiagnosticNative1600Controller'](),s,b

def generate(output,base_read_latency=20):
 output.mkdir(exist_ok=False,parents=True)
 from migen.fhdl import verilog
 dut,s,b=make_controller(base_read_latency);rtl=str(verilog.convert(dut,ios=dut.ios,name='litedram_external_controller'))
 rtl,n=re.subn(r'^(reg \[\d+:0\] storage(?:_\d+)?\[0:\d+\];)$',lambda m:'(* ramstyle = "MLAB, no_rw_check" *) '+m[1],rtl,flags=re.M);assert n==35,n
 (output/'litedram_external_controller.v').write_text(rtl);(output/'generator_snapshot.py').write_text(s)
 sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();timing={k:getattr(dut.memory_timing,k) for k in ['tRP','tRCD','tWR','tWTR','tREFI','tRFC','tFAW','tCCD','tRRD','tRC','tRAS','tZQCS']}
 (output/'profile.json').write_text(json.dumps({'controller_hz':200000000,'ck_hz':800000000,'ref_hz':200000000,'cl':12,'cwl':11,'rdphase':dut.phy.settings.rdphase,'wrphase':dut.phy.settings.wrphase,'base_read_latency':base_read_latency,'frontend_read_latency':dut.phy.settings.read_latency,'geometry':{'rowbits':17,'colbits':10,'bankbits':4,'bus_bits':32,'rank_count':1,'capacity_bytes':8589934592},'timing_cycles':timing,'tRFC_design_ns':600,'refresh_interval_ns':3906.25,'phy_sha256':sha(HERE/'phy_native1600_v1.py'),'generator_sha256':sha(Path(__file__)),'parent_generator_sha256':sha(V8/'generate_external_v8_ddr2666.py'),'rtl_sha256':sha(output/'litedram_external_controller.v'),'hardware_qualified':False,'read_age_anchor':{'native333_gate5':18,'native200_gate3':16,'same_registered_pipeline':True},'return_latency_pending_DFI_validation':True},indent=2)+'\n');print(output,timing)
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--output',default=str(HERE/'generated-draft'));a.add_argument('--base-read-latency',type=int,default=20);o=a.parse_args();generate(Path(o.output),o.base_read_latency)
