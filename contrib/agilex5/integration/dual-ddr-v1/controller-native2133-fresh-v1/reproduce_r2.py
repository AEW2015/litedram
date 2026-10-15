"""Second isolated generation plus actual imported Python source closure."""
from pathlib import Path
import sys,json,hashlib,subprocess
from generate import generate
from layer_repairs import local_resets,first_error_and_fifo,v11_layers
N=Path(__file__).resolve().parent;R=N.parents[2];D=N/'generated-repro-r2';generate(D,23)
s,reset=local_resets((D/'litedram_external_controller_raw.v').read_text());s=first_error_and_fifo(s);s,repair=v11_layers(s);(D/'litedram_external_controller.v').write_text(s)
h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();compared={}
for name in ['litedram_external_controller_raw.v','generator_snapshot.py','profile.json','litedram_external_controller.v']:
 a=N/'generated-base23'/name;b=D/name;assert a.read_bytes()==b.read_bytes(),name;compared[name]=h(a)
files=set()
for module in list(sys.modules.values()):
 p=getattr(module,'__file__',None)
 if p:
  q=Path(p).resolve()
  if q.is_file():files.add(q)
files.update([R/'config/board.json',N/'prepare_phy.py',N/'layer_repairs.py',R/'integration/dual-ddr-v1/controller-native1600-v1/validity_patch.py',R/'integration/dual-ddr-v1/controller-fast-v11/prepare.py',R/'integration/dual-ddr-v1/controller-fast-v11/changes.json',R/'build/dual_ddr_opentpu_v11/litedram_external_controller.v',R/'reports/DDR2133_INTERMEDIATE_PROFILE_AUDIT_V1.md'])
pins={str(p.relative_to(R)) if p.is_relative_to(R) else str(p):h(p) for p in sorted(files)}
(N/'generation-dependencies.json').write_text(json.dumps({'prototype_only':True,'python_version':sys.version,'python_executable':sys.executable,'python_executable_sha256':h(Path(sys.executable).resolve()),'actual_imported_module_file_count':len(files),'sha256':pins,'reproduced':compared},indent=2)+'\n');print('GENERATION_REPRO_PASS',len(files))
