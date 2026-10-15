"""Unselected DDR2133 fresh-valid controller; no frozen inputs changed."""
from pathlib import Path
import importlib.util,sys,re,json,hashlib,argparse
N=Path(__file__).resolve().parent;R=N.parents[2];P=R/'integration/dual-ddr-v1/controller-native1600-v1/generate.py';sys.path.insert(0,str(N))
spec=importlib.util.spec_from_file_location('parent1600_recipe',P);parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
def transformed(base_read_latency=23):
 s,b=parent.transformed(base_read_latency)
 old='from phy_native1600_v1 import Agilex5Native1600PHY as Agilex5Primitive2933PHY';assert s.count(old)==1;s=s.replace(old,'from phy_native2133_fresh_v1 import Agilex5Native2133FreshPHY as Agilex5Primitive2933PHY')
 s=s.replace('memory_clock_mhz=800','memory_clock_mhz=1067').replace('memory_clock_mhz == 800','memory_clock_mhz == 1067').replace('memory_clock_mhz==800','memory_clock_mhz==1067')
 s=s.replace('800000000.0','(3200000000.0/3)').replace('800000000','(3200000000/3)')
 s=s.replace('cl,cwl=12,11','cl,cwl=16,11').replace('tCCD=(5,None)','tCCD=(6,None)').replace('tWR=15','tWR=20/(3200000000/3)*1e9').replace('math.ceil(12/4)','math.ceil(20/4)').replace('math.ceil(5/4)','math.ceil(6/4)')
 s=s.replace('BoardNative1600Memory','BoardNative2133Memory').replace('DiagnosticNative1600Controller','DiagnosticNative2133Controller').replace("speedgrade='1600'","speedgrade='2133'").replace('"1600"','"2133"')
 return s,b

def make_controller(base_read_latency=23):
 s,b=transformed(base_read_latency);ns={'__file__':str(b)};exec(compile(s,str(b),'exec'),ns);return ns['DiagnosticNative2133Controller'](),s,b

def generate(output,base_read_latency=23):
 output.mkdir(parents=True,exist_ok=False)
 from migen.fhdl import verilog
 dut,s,b=make_controller(base_read_latency);rtl=str(verilog.convert(dut,ios=dut.ios,name='litedram_external_controller'))
 rtl,n=re.subn(r'^(reg \[\d+:0\] storage(?:_\d+)?\[0:\d+\];)$',lambda m:'(* ramstyle = "MLAB, no_rw_check" *) '+m[1],rtl,flags=re.M);assert n==35
 (output/'litedram_external_controller_raw.v').write_text(rtl);(output/'generator_snapshot.py').write_text(s)
 timing={k:getattr(dut.memory_timing,k) for k in ['tRP','tRCD','tWR','tWTR','tREFI','tRFC','tFAW','tCCD','tRRD','tRC','tRAS','tZQCS']};h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
 profile={'prototype_only':True,'selected_hardware_seal':False,'hardware_qualified':False,'ref_hz':'800000000/3','CK_hz':'3200000000/3','native_hz':'800000000/3','CL':16,'CWL':11,'MR0':'0xb34','MR2':'0x210','MR0_WR_CK':20,'MR0_RTP_CK':10,'tCCD_L_CK':6,'rdphase':dut.phy.settings.rdphase,'wrphase':dut.phy.settings.wrphase,'base_read_latency':base_read_latency,'frontend_read_latency':dut.phy.settings.read_latency,'timing_cycles':timing,'tRFC_design_ns':600,'refresh_interval_ns':3906.25,'geometry':{'rowbits':17,'colbits':10,'bankbits':4,'bus_bits':32,'ranks':1,'capacity_bytes':8589934592},'hard_return_age_untrained':True,'requires_V11_layers':True,'source_sha256':{str(p.relative_to(R)):h(p) for p in [Path(__file__).resolve(),N/'phy_native2133_fresh_v1.py',P,R/'reports/DDR2133_INTERMEDIATE_PROFILE_AUDIT_V1.md']},'raw_RTL_sha256':hashlib.sha256(rtl.encode()).hexdigest()}
 (output/'profile.json').write_text(json.dumps(profile,indent=2)+'\n');print('GENERATED2133',timing)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--base-read-latency',type=int,default=23);a=p.parse_args();generate(Path(a.output),a.base_read_latency)
