#!/usr/bin/env python3
"""Isolate actual RTL IDs and Tcl helper/global state without altering keywords."""
from pathlib import Path
import re,json,hashlib
R=Path(__file__).resolve().parents[3];src=R/'rtl/primitive_backend/1333';dst=R/'integration/dual-ddr-v1/phy-bank3-r3'
assert not dst.exists(),'preserve generated evidence'
files=[p for p in src.rglob('*') if p.is_file() and p.name!='EXTRACTION_MANIFEST.json']
ids=set();procs=set()
for p in files:
 s=p.read_text()
 if p.suffix in ['.v','.sv']:
  clean=re.sub(r'/\*.*?\*/|//[^\n]*','',s,flags=re.S)
  ids.update(re.findall(r'(?m)^\s*module\s+([A-Za-z_]\w*)',clean))
 if p.suffix in ['.tcl','.sdc']:
  procs.update(re.findall(r'(?m)^\s*proc\s+(\w+)',s))
assert not {'as','min','max'} & ids
# These long generated identities are also prefixes of filenames and specific
# GLOBAL/cache/database identifiers. Prefix-match exactly these long names only.
ips={x for x in ids if x.startswith('phy_probe_phy_phylite_')}
assert len(ips)==2
repl={x:'dual3_'+x for x in ids|procs if x not in ['min','max']}
repl.update({'phylite_arch_fp_0':'dual3_arch','phylite_ph2_941':'dual3_ph2'})
parts=[re.escape(x) if x in ips else r'\b'+re.escape(x)+r'\b' for x in sorted(repl,key=len,reverse=True)]
pattern=re.compile('|'.join(parts))
def rewrite(s):return pattern.sub(lambda m:repl[m[0]],s)
ledger={}
for p in files:
 rel=Path(rewrite(str(p.relative_to(src))));q=dst/rel;q.parent.mkdir(parents=True,exist_ok=True)
 t=rewrite(p.read_text())
 if rel.name.endswith('_mgxjhyq.v'):
  def group(m):
   i,g=int(m[1]),int(m[2]);lane=i//12
   if lane==0:assert g==1;g=0
   if lane==4:assert g==0;g=1
   return f'.MAP_INDEX_TO_GROUP_{i} ({g})'
  t,n=re.subn(r'\.MAP_INDEX_TO_GROUP_(\d+)\s*\((-?\d+)\)',group,t);assert n==96
 # Regression for the precise corruption discovered in failed R2 preparation.
 assert not any(x in t for x in ['-phdual3_ase','-mdual3_aster','-nocdual3_ase','tidual3_ming'])
 q.write_text(t);ledger[str(rel)]=hashlib.sha256(q.read_bytes()).hexdigest()
record={'parent':str(src.relative_to(R)),'parent_manifest':'EXTRACTION_MANIFEST.json is historical donor evidence and is not copied',
 'profile':'M5/N1 ref266.666667MHz CK1333.333333MHz native333.333333MHz',
 'data_lanes':[0,4,5,6],'ECC':'notconnected x32 only','actual_module_ids':sorted(ids),
 'renames':repl,'explicit_extended_prefixes':sorted(ips),
 'shared_helpers':['min','max (identical pure Tcl arithmetic; expression functions remain untouched)','::io_only_analysis (vendor global mode)'],
 'files':ledger}
(dst/'DUAL_DDR_CLONE_MANIFEST.json').write_text(json.dumps(record,indent=2)+'\n')
print(dst)
