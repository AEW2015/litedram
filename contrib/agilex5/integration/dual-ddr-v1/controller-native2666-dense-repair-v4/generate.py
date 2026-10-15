from pathlib import Path
import importlib.util,sys,re,json,hashlib,argparse
N=Path(__file__).resolve().parent;R=N.parents[2];P=R/'integration/dual-ddr-v1/controller-native2666-fresh-v1';sys.path.insert(0,str(P));sys.path.insert(0,str(N))
def load(name,p):
 spec=importlib.util.spec_from_file_location(name,p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
parent=load('dense_original_recipe',P/'generate.py');layers=load('dense_original_layers',P/'layer_repairs.py')
def make_controller():
 s,b=parent.transformed(25)
 s=s.replace('from litedram.modules import MT40A2G8, _SpeedgradeTimings','from litedram.modules import MT40A2G8, MT40A2G16, _SpeedgradeTimings, _TechnologyTimings')
 s=s.replace('class Board2933Memory(MT40A2G8):','class Board2933Memory(MT40A2G16):')
 a=s.index('    speedgrade_timings =');z=s.index('\nclass Diagnostic2933Controller',a)
 s=s[:a]+'''    technology_timings = _TechnologyTimings(
        tREFI={"1x":3906.25,"2x":1953.125,"4x":976.5625},
        tWTR=(8,10),tCCD=(8,None),tRRD=(8,10),tZQCS=(128,160))
    speedgrade_timings={"2933":_SpeedgradeTimings(
        tRP=20,tRCD=20,tWR=20/(4000000000/3)*1e9,
        tRFC={"1x":(None,600),"2x":(None,600),"4x":(None,600)},
        tFAW=(32,40),tRAS=40)}
    speedgrade_timings["default"]=speedgrade_timings["2933"]
'''+s[z:]
 s=s.replace('math.floor(7.8e-6*controller_clock_hz)','math.floor(3.90625e-6*controller_clock_hz)')
 s=s.replace('math.ceil(24/4)','math.ceil(20/4)')
 for a,z in [('from phy_native2666_fresh_v1 import Agilex5Native2666FreshPHY as Agilex5Primitive2933PHY','from dense_phy import Agilex5Native2666DensePHY as Agilex5Primitive2933PHY'),('litedram_phy.fullrate_crossbar_v4','dense_crossbar'),('litedram_phy.fullrate_multiplexer_unified_v32','dense_mux')]:
  assert a in s;s=s.replace(a,z)
 s=s.replace('phy.clear_capture.eq(clear_capture)','phy.clear_capture.eq(dram.controller.interface.clear_accept),dram.controller.interface.rdata_clear_request.eq(clear_capture)')
 s=s.replace('42:phy.command_conflict','42:phy.command_conflict | ((phy.frame_assembly_fault | dram.controller.interface.return_fault)<<4)')
 s=s.replace('locked_slow.eq(phy.locked),raw_slow.eq(raw_capture)]',
     'locked_slow.eq(phy.locked),If(reduce(or_,[p.rddata_valid for p in phy.dfi.phases]),raw_slow.eq(raw_capture))]')
 s=s.replace('mask_error_slow.eq(phy.mask_error | mask_guard.error)','mask_error_slow.eq(phy.mask_error | mask_guard.error | phy.frame_assembly_fault | dram.controller.interface.return_fault)')
 ns={'__file__':str(b)};exec(compile(s,str(b), 'exec'),ns);return ns['Diagnostic2933Controller'](),s
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();d=Path(a.output);d.mkdir(parents=True,exist_ok=False)
 from migen.fhdl import verilog
 dut,s=make_controller();rtl=str(verilog.convert(dut,ios=dut.ios,name='litedram_external_controller'));rtl,n=re.subn(r'^(reg \[\d+:0\] storage(?:_\d+)?\[0:\d+\];)$',lambda m:'(* ramstyle = "MLAB, no_rw_check" *) '+m[1],rtl,flags=re.M);assert n==35,n
 (d/'raw.v').write_text(rtl);(d/'snapshot.py').write_text(s);rtl,reset=layers.local_resets(rtl);rtl=layers.first_error_and_fifo(rtl);rtl,repair=layers.v11_layers(rtl);(d/'litedram_external_controller.v').write_text(rtl)
 deps={str(Path(m.__file__).resolve()):hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in list(sys.modules.values()) if getattr(m,'__file__',None) and Path(m.__file__).is_file() and Path(m.__file__).suffix=='.py'}
 (d/'profile.json').write_text(json.dumps({'SOURCE_SIM_ONLY':True,'selected':False,'hardware_qualified':False,'base_latency':25,'front_latency':27,'registered_phase_input_cycles':1,'registered_return_input_cycles':1,'prefetched_head_read_cycles':1,'prefetch_has_no_external_cycle_penalty':True,'alignment_required':1,'CL':20,'CWL':14,'rdphase':3,'wrphase':2,'phase_sample_queue_depth':2,'CAS_ordered_return_depth':16,'CSR42_new_fault_bit':4,'fault_recovery':'Explicitnewclear-edgeonlynativepending/count/start/dueidle; faultheldthroughactiveorheldclear; sys_rstalsoflushes. Nativependingretiresatfixeddeadline, missingframefaultdoesnotpoisonlaterquiescenttraining.','ref_hz':'800000000/3','CK_hz':'4000000000/3','native_hz':'1000000000/3','DDR_MT_s':'8000/3','MR0':'0xb44','MR2':'0x220','MR0_WR_CK':20,'MR0_RTP_CK':10,'tCCD_L_CK':8,'tRFC_design_ns':600,'refresh_interval_ns':3906.25,'geometry':{'rowbits':17,'colbits':10,'bankbits':4,'bus_bits':32,'ranks':1,'capacity_bytes':8589934592},'timing_cycles':{k:getattr(dut.memory_timing,k) for k in ['tRP','tRCD','tWR','tWTR','tREFI','tRFC','tFAW','tCCD','tRRD','tRC','tRAS','tZQCS']},'reset':reset,'V11':repair,'dependency_sha256':deps,'RTL_sha256':hashlib.sha256(rtl.encode()).hexdigest()},indent=2)+'\n')
 print('SOURCE_DENSE_GENERATED',d)
