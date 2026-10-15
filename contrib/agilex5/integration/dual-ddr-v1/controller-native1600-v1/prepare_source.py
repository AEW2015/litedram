#!/usr/bin/env python3
"""Prepare isolated native200 PHY policy. Never changes an existing source."""
from pathlib import Path
import json,hashlib
R=Path(__file__).resolve().parents[3];N=Path(__file__).resolve().parent;p=N/'phy_native1600_v1.py';assert not p.exists()
s=(R/'litedram_phy/agilex5_fullrate_v22.py').read_text().replace('class Agilex5FullRatePHYV22','class Agilex5Native1600PHY').replace('cl=22, cwl=16','cl=12, cwl=11').replace('read_latency=22','read_latency=20').replace('assert (cl, cwl) in ((18,12),(20,14),(22,16))','assert (cl,cwl)==(12,11) and read_latency>=1').replace("phytype='Agilex5Primitive3200PHY'","phytype='Agilex5Native1600V1'").replace('self.rd_delay=Signal(4,reset=2)','self.rd_delay=Signal(4,reset=3)')
# Preserve the physically proven V22 high-pipeline structure at the new CL.
for old in ['if cl>=16:','if cl>=14:']:s=s.replace(old,'if True: # Frozen V22 physical control-pipeline policy.')
s=s.replace("Path(__file__).resolve().parents[1]/'config/board.json'","Path(__file__).resolve().parents[3]/'config/board.json'")
from validity_patch import apply
s=apply(s)
p.write_text(s)
(N/'phy_policy.json').write_text(json.dumps({'parent':'litedram_phy/agilex5_fullrate_v22.py','parent_sha256':hashlib.sha256((R/'litedram_phy/agilex5_fullrate_v22.py').read_bytes()).hexdigest(),'physical_pipeline':'preserves all four high-CL structural branches; no physical control latency change','cl':12,'cwl':11,'rdphase':3,'wrphase':1,'base_read_latency_candidate':20,'frontend_read_latency_candidate':22,'read_gate_reset':3,'requires_measured_age_and_DFI_validation':True},indent=2)+'\n')
