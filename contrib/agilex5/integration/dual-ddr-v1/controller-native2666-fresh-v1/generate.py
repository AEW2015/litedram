"""Fresh-valid high-rate model from original V8 generator; final V11 layers added separately."""
from pathlib import Path
import importlib.util,sys,re,json,hashlib,argparse
N=Path(__file__).resolve().parent;R=N.parents[2];V8=R/'integration/opentpu-v1/controller-v8-ddr2666';sys.path.insert(0,str(V8));sys.path.insert(0,str(N))
spec=importlib.util.spec_from_file_location('native2666_parent',V8/'generate_external_v8_ddr2666.py');parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
def transformed(base_read_latency=22):
 s,b=parent.transformed();old='from litedram_phy.agilex5_fullrate_v22 import Agilex5FullRatePHYV22 as Agilex5Primitive2933PHY';assert s.count(old)==1;s=s.replace(old,'from phy_native2666_fresh_v1 import Agilex5Native2666FreshPHY as Agilex5Primitive2933PHY')
 assert s.count('read_latency=22')==1;s=s.replace('read_latency=22',f'read_latency={int(base_read_latency)}');return s,b
def make_controller(base_read_latency=22):
 s,b=transformed(base_read_latency);ns={'__file__':str(b)};exec(compile(s,str(b),'exec'),ns);return ns['Diagnostic2933Controller'](),s,b
def generate(output,base_read_latency=22):
 output.mkdir(parents=True,exist_ok=False)
 from migen.fhdl import verilog
 dut,s,b=make_controller(base_read_latency);rtl=str(verilog.convert(dut,ios=dut.ios,name='litedram_external_controller'))
 rtl,n=re.subn(r'^(reg \[\d+:0\] storage(?:_\d+)?\[0:\d+\];)$',lambda m:'(* ramstyle = "MLAB, no_rw_check" *) '+m[1],rtl,flags=re.M);assert n==35,n
 (output/'litedram_external_controller_raw.v').write_text(rtl);(output/'generator_snapshot.py').write_text(s)
 (output/'profile.json').write_text(json.dumps({'prototype_only':True,'no_hardware_or_fit_qualification':True,'CL':20,'CWL':14,'CK_Hz':'4000000000/3','native_Hz':'1000000000/3','rdphase':dut.phy.settings.rdphase,'wrphase':dut.phy.settings.wrphase,'base_read_latency':base_read_latency,'front_read_latency':dut.phy.settings.read_latency,'PHY_sha256':hashlib.sha256((N/'phy_native2666_fresh_v1.py').read_bytes()).hexdigest(),'raw_RTL_sha256':hashlib.sha256(rtl.encode()).hexdigest(),'requires_V11_repair_layers_and_tests':True},indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--base-read-latency',type=int,default=22);a=p.parse_args();generate(Path(a.output),a.base_read_latency)
