"""Isolated CL16 fresh-valid PHY; no existing file is rewritten."""
from pathlib import Path
import importlib.util,json,hashlib
N=Path(__file__).resolve().parent;R=N.parents[2];P=R/'litedram_phy/agilex5_fullrate_v22.py';patch=R/'integration/dual-ddr-v1/controller-native1600-v1/validity_patch.py'
spec=importlib.util.spec_from_file_location('proven_validity_patch',patch);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
s=P.read_text().replace('class Agilex5FullRatePHYV22','class Agilex5Native2133FreshPHY').replace('cl=22, cwl=16','cl=16, cwl=11').replace('assert (cl, cwl) in ((18,12),(20,14),(22,16))','assert (cl,cwl)==(16,11) and read_latency>=1').replace("phytype='Agilex5Primitive3200PHY'","phytype='Agilex5Native2133FreshV1'").replace("Path(__file__).resolve().parents[1]/'config/board.json'","Path(__file__).resolve().parents[3]/'config/board.json'")
s=s.replace('        physical=Interface(17,4,1,64,4)\n','        physical=Interface(17,4,1,64,4)\n        self.physical_dfi=physical\n')
s=m.apply(s).replace('read_latency=22','read_latency=23');p=N/'phy_native2133_fresh_v1.py';assert not p.exists();p.write_text(s)
(N/'phy_lineage.json').write_text(json.dumps({'parent':str(P.relative_to(R)),'parent_sha256':hashlib.sha256(P.read_bytes()).hexdigest(),'proven_validity_patch':str(patch.relative_to(R)),'proven_validity_patch_sha256':hashlib.sha256(patch.read_bytes()).hexdigest(),'physical_command_payload_pipeline_unchanged':True,'CL':16,'CWL':11,'base_read_latency_default':23,'front_read_latency_default':25,'read_delay_reset_retained':2,'write_delay_reset_retained':2,'manual_MPR_raw_payload_path_unchanged':True,'prototype_only':True},indent=2)+'\n')
