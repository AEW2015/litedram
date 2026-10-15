#!/usr/bin/env python3
"""V11 fresh synthesis only. Copy no V10 QDB or fitted checkpoint."""
from pathlib import Path
import hashlib,json
R=Path(__file__).resolve().parents[4];I=R/'integration/dual-ddr-v1';N=I/'opentpu-next';A=R/'build/dual_ddr_opentpu_v10';B=R/'build/dual_ddr_opentpu_v11';sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert not B.exists();B.mkdir()
for name in ['memory_test.qpf','quartus.ini','public_litedram.sdc']:(B/name).write_bytes((A/name).read_bytes())
(B/'litedram_external_controller.v').write_bytes((I/'controller-fast-v11/litedram_external_controller.v').read_bytes())
q=(A/'memory_test.qsf').read_text()
replacements={
 '../../integration/dual-ddr-v1/rtl-fast-v10/dual_ddr_cdc_v10.sdc':'../../integration/dual-ddr-v1/rtl-fast-v11/dual_ddr_cdc_v11.sdc',
 '../../integration/dual-ddr-v1/opentpu-next/rtl-fast-repair-r1/dual_ddr_csr_router_fast_r1.sv':'../../integration/dual-ddr-v1/opentpu-next/rtl-fast-v11/dual_ddr_csr_router_fast_v11.sv',
 '../../integration/dual-ddr-v1/opentpu-next/rtl-fast-repair-r1/dual_ddr_opentpu_top.sv':'../../integration/dual-ddr-v1/opentpu-next/rtl-fast-v11/dual_ddr_opentpu_top.sv'}
for bank in [0,1]:replacements[f'../../integration/dual-ddr-v1/opentpu-next/rtl-physical-r5/dual_ddr_opentpu_bank{bank}_path.sv']=f'../../integration/dual-ddr-v1/opentpu-next/rtl-fast-v11/dual_ddr_opentpu_bank{bank}_path.sv'
for old,new in replacements.items():assert q.count(old)==1,old;q=q.replace(old,new)
(B/'memory_test.qsf').write_text(q)
oldledger=json.loads((A/'dual_ddr_inputs.json').read_text());files={R/k for k in oldledger if not k.startswith(str(A.relative_to(R))+'/')};files.add(Path(__file__).resolve());files.update(R/new[6:] for new in replacements.values());files.update(p for p in (I/'controller-fast-v11').iterdir() if p.is_file() and p.suffix in ['.py','.v','.json']);files.update(p for p in (I/'controller-fast-v11/peer').iterdir() if p.is_file() and p.suffix in ['.sv','.v','.json','.sh','.log']);files.update(p for p in (I/'controller-fast-v11/sim').iterdir() if p.is_file() and p.suffix in ['.py','.sv','.v','.log','.json']);files.update(p for p in (N/'sim-fast-router-v11').iterdir() if p.is_file() and p.suffix in ['.sv','.log','.json']);files.update(p for p in (I/'sim-sdc-v11').iterdir() if p.is_file());files.update(p for p in B.iterdir() if p.is_file())
ledger={str(p.relative_to(R)):sha(p) for p in sorted(files)};(B/'dual_ddr_inputs.json').write_text(json.dumps(ledger,indent=2)+'\n');assert not (B/'qdb').exists()
(B/'UNQUALIFIED.json').write_text(json.dumps({'parent':str(A.relative_to(R)),'inputs_sha256':sha(B/'dual_ddr_inputs.json'),'input_count':len(ledger),'fresh_synthesis_required':True,'clock_pin_native_pipeline_changes':False,'diagnostic_CSR_read_extra_cycles':1,'changes':['local accepted-command payload enable pulses','dedicated same-edge config-address copy','local WE selection and first_bad-start replicas','9 registered read mux partitions of62terms with aligned CSR valid'],'hardware_qualified':False},indent=2)+'\n');print(B,len(ledger),sha(B/'dual_ddr_inputs.json'))
