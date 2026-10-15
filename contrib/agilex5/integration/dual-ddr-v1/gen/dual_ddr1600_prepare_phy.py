#!/usr/bin/env python3
from pathlib import Path
import importlib.util,shutil,json,hashlib,re
R=Path(__file__).resolve().parents[3];I=R/'integration/dual-ddr-v1';N=I/'ddr1600-diag-v1';N.mkdir(exist_ok=True)
spec=importlib.util.spec_from_file_location('extract',R/'scripts/extract_primitive_backend.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
source=R/'build/strobe-hybrid-800/ip/phy_probe/phy_probe_phy';A=N/'phy-bank0';assert not A.exists();shutil.copytree(source,A);m.prune_qip(A/'phy_probe_phy.qip');assert not m.check_qip(A,A/'phy_probe_phy.qip')
s=(I/'gen/dual_ddr_phy_clone_r3.py').read_text().replace("src=R/'rtl/primitive_backend/1333';dst=R/'integration/dual-ddr-v1/phy-bank3-r3'","src=R/'integration/dual-ddr-v1/ddr1600-diag-v1/phy-bank0';dst=R/'integration/dual-ddr-v1/ddr1600-diag-v1/phy-bank1'")
s=s.replace("if rel.name.endswith('_mgxjhyq.v'):","if rel.suffix=='.v' and 'phylite_ph2_941_' in rel.name:")
s=s.replace("M5/N1 ref266.666667MHz CK1333.333333MHz native333.333333MHz","M4/N1 ref200MHz CK800MHz native200MHz")
exec(compile(s,str(I/'gen/dual_ddr_phy_clone_r3.py'),'exec'),{'__file__':str(I/'gen/dual_ddr_phy_clone_r3.py'),'__name__':'__main__'})
B=N/'phy-bank1';sv=next((B/'dual3_ph2/synth').glob('*.v'));tcl=next((B/'dual3_arch/synth').glob('*timing_ip_parameters.tcl'));t=tcl.read_text()
for n,v in re.findall(r'\.MAP_INDEX_TO_GROUP_(\d+)\s*\((-?\d+)\)',sv.read_text()):t=re.sub(r'(set var\(MAP_INDEX_TO_GROUP_'+n+r'\)\s+)-?\d+',lambda x:x[1]+v,t)
tcl.write_text(t)
for p in (B/'dual3_arch/synth').glob('*timing.sdc'):
 t=p.read_text().replace('"group_${i}_lane_${n}_dqsn_nff_clk"','"${inst}_group_${i}_lane_${n}_dqsn_nff_clk"');p.write_text(t)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
for bank in [A,B]:
 (bank/'DIAG_EXTRACTION_MANIFEST.json').write_text(json.dumps({'source':str(source.relative_to(R)),'profile':'vendor generated ref200 M4N1 CK800/core200 four phase','manual_only':True,'files':{str(p.relative_to(bank)):sha(p) for p in sorted(bank.rglob('*')) if p.is_file() and p.name not in ['DUAL_DDR_CLONE_MANIFEST.json','DIAG_EXTRACTION_MANIFEST.json']}},indent=2)+'\n')
print('Four-phase800 PHY pair prepared; no hardware qualification')
