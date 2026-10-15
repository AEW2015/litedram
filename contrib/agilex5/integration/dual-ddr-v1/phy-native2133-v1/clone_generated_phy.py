#!/usr/bin/env python3
"""Namespace one successfully generated PHYLite IP output into bank1."""
from pathlib import Path
import hashlib,json,re,shutil
ROOT=Path(__file__).resolve().parents[3]
HERE=ROOT/'integration/dual-ddr-v1/phy-native2133-v1'
SRC=HERE/'generated/bank0/phy_probe_phy'
SRCIP=HERE/'generated/bank0/phy_probe_phy.ip'
DSTROOT=HERE/'generated/bank1'
DST=DSTROOT/'dual3_phy_probe_phy'
assert SRC.is_dir() and SRCIP.is_file()
if DSTROOT.exists():
    shutil.rmtree(DSTROOT)
DST.mkdir(parents=True)
files=[p for p in SRC.rglob('*') if p.is_file()]
mods=set();procs=set()
for p in files:
    try:s=p.read_text()
    except UnicodeDecodeError:continue
    if p.suffix.lower() in {'.v','.sv'}:
        clean=re.sub(r'/\*.*?\*/|//[^\n]*','',s,flags=re.S)
        mods.update(re.findall(r'(?m)^\s*module\s+([A-Za-z_]\w*)',clean))
    if p.suffix.lower() in {'.tcl','.sdc'}:
        procs.update(re.findall(r'(?m)^\s*proc\s+(\w+)',s))
long_ids={x for x in mods if x.startswith('phy_probe_phy_phylite_')}
repl={x:'dual3_'+x for x in mods|procs if x not in {'min','max'}}
repl.update({'phy_probe_phy':'dual3_phy_probe_phy','phylite_arch_fp_0':'dual3_arch','phylite_ph2_941':'dual3_ph2'})
parts=[re.escape(x) if x in long_ids else r'\b'+re.escape(x)+r'\b' for x in sorted(repl,key=len,reverse=True)]
pat=re.compile('|'.join(parts))
def tr(s):return pat.sub(lambda m:repl[m[0]],s)
ledger={}
for p in files:
    rel=Path(tr(str(p.relative_to(SRC))))
    q=DST/rel;q.parent.mkdir(parents=True,exist_ok=True)
    try:q.write_text(tr(p.read_text()))
    except UnicodeDecodeError:shutil.copy2(p,q)
    ledger[str(rel)]=hashlib.sha256(q.read_bytes()).hexdigest()
iptext=tr(SRCIP.read_text())
(DSTROOT/'dual3_phy_probe_phy.ip').write_text(iptext)
ledger['../dual3_phy_probe_phy.ip']=hashlib.sha256((DSTROOT/'dual3_phy_probe_phy.ip').read_bytes()).hexdigest()
# Verify bank1's module namespace is disjoint from bank0, except deliberate pure
# Tcl math routines (they are not Verilog modules and remain source-identical).
newmods=set()
for p in DST.rglob('*'):
    if p.suffix.lower() not in {'.v','.sv'}:continue
    clean=re.sub(r'/\*.*?\*/|//[^\n]*','',p.read_text(),flags=re.S)
    newmods.update(re.findall(r'(?m)^\s*module\s+([A-Za-z_]\w*)',clean))
assert mods.isdisjoint(newmods), sorted(mods&newmods)
assert 'dual3_phy_probe_phy' in newmods and (DSTROOT/'dual3_phy_probe_phy.ip').is_file()
meta={'module_ids_bank0':sorted(mods),'module_ids_bank1':sorted(newmods),'renames':repl,'file_sha256':ledger}
(HERE/'bank1-namespace.json').write_text(json.dumps(meta,indent=2)+'\n')
