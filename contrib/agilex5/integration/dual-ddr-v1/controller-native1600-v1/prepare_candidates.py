#!/usr/bin/env python3
"""Prepare isolated native200 memory/full builds after independent PHY proof."""
from pathlib import Path
import json,hashlib,argparse
R=Path(__file__).resolve().parents[3];I=R/'integration/dual-ddr-v1';N=Path(__file__).resolve().parent
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def prepare(kind,generated):
 full=kind=='full';A=R/('build/dual_ddr_opentpu_v7' if full else 'build/dual_ddr2666_v12');B=R/('build/dual_ddr1600_opentpu_native_v1' if full else 'build/dual_ddr1600_native_v1');P=N/('full' if full else 'memory');assert not B.exists() and not P.exists();P.mkdir();B.mkdir()
 for name in ['memory_test.qpf','quartus.ini']:(B/name).write_bytes((A/name).read_bytes())
 (B/'litedram_external_controller.v').write_bytes((generated/'litedram_external_controller_local_reset.v').read_bytes())
 (B/'public_litedram.sdc').write_text((A/'public_litedram.sdc').read_text().replace('3.750000000','5.000000000'))
 sd=P/'dual_ddr_native1600_cdc.sdc';sd.write_text((I/'rtl-r11/dual_ddr_cdc_r11.sdc').read_text().replace('::dual_ddr_memory_r11','::dual_native1600_'+kind).replace('3.750000000','5.000000000'))
 q=(A/'memory_test.qsf').read_text().replace('../../rtl/primitive_backend/1333/phy_probe_phy.qip','../../integration/dual-ddr-v1/ddr1600-diag-v1/phy-bank0/phy_probe_phy.qip').replace('../../integration/dual-ddr-v1/phy-bank3-r5/dual3_phy_probe_phy.qip','../../integration/dual-ddr-v1/ddr1600-diag-v1/phy-bank1/dual3_phy_probe_phy.qip').replace('../../integration/dual-ddr-v1/rtl-r11/dual_ddr_cdc_r11.sdc','../../'+str(sd.relative_to(R)))
 if full:
  src=I/'opentpu-next/rtl/dual_ddr_opentpu_platform.sv';dst=P/src.name;dst.write_text(src.read_text().replace('.DDR_MTS(2667)','.DDR_MTS(1600)'));assert '.DDR_MTS(1600)' in dst.read_text();q=q.replace('../../'+str(src.relative_to(R)),'../../'+str(dst.relative_to(R)))
 (B/'memory_test.qsf').write_text(q)
 old=json.loads((A/'dual_ddr_inputs.json').read_text());files={R/k for k in old if not k.startswith(str(A.relative_to(R))+'/')};files.update(p for p in (I/'ddr1600-diag-v1').rglob('*') if p.is_file());files.update(p for p in P.rglob('*') if p.is_file());files.update(p for p in generated.iterdir() if p.is_file());files.update(p for p in N.iterdir() if p.is_file() and p.suffix=='.py');files.update(p for p in B.iterdir() if p.is_file());ledger={str(p.relative_to(R)):sha(p) for p in sorted(files) if '__pycache__' not in p.parts and p.suffix!='.pyc'};(B/'dual_ddr_inputs.json').write_text(json.dumps(ledger,indent=2)+'\n');(B/'NATIVE1600_UNQUALIFIED.json').write_text(json.dumps({'kind':kind,'native_hz':200000000,'ref_hz':200000000,'ck_hz':800000000,'cl':12,'cwl':11,'hardware_qualified':False,'input_count':len(ledger),'inputs_sha256':sha(B/'dual_ddr_inputs.json')},indent=2)+'\n');print(B,len(ledger),sha(B/'dual_ddr_inputs.json'))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('kind',choices=['memory','full']);p.add_argument('--generated',required=True);a=p.parse_args();prepare(a.kind,Path(a.generated).resolve())
