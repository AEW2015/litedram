#!/usr/bin/env python3
"""Resolved DDR2666 controller settings against preserved qualified2666 table."""
import hashlib,json,re
from pathlib import Path
from generate_external_v8_ddr2666 import make_controller
D,_,_=make_controller();I=Path(__file__).resolve().parent;R=I.parents[2]
keys=['tRP','tRCD','tWR','tWTR','tREFI','tRFC','tFAW','tCCD','tRRD','tRC','tRAS','tZQCS']
timings={k:getattr(D.memory_timing,k) for k in keys}
old=R/'build/dma-fullrate-2666-timing-v24/controller.json';o=json.loads(old.read_text())
assert timings==o['timing_cycles'],(timings,o['timing_cycles'])
p=D.phy.settings
assert (p.cl,p.cwl,p.rdphase,p.wrphase,p.read_latency,p.write_latency)==(20,14,3,2,24,2)
result={'all_pass':True,'controller_clock_hz':1e9/3,'memory_clock_hz':4e9/3,'reference_clock_hz':8e8/3,'timing_cycles':timings,'historical_timing_table':str(old.relative_to(R)),'historical_table_sha256':hashlib.sha256(old.read_bytes()).hexdigest(),'cl':p.cl,'cwl':p.cwl,'rdphase':p.rdphase,'wrphase':p.wrphase,'frontend_read_latency':p.read_latency,'base_read_latency':22,'explicit_dfi_output_pipeline_cycles':2,'write_latency':p.write_latency,'controller_rtl_sha256':hashlib.sha256((I/'litedram_external_controller.v').read_bytes()).hexdigest(),'vendor_frequency_status':'experimental unsafe/nonworking generation switch','calibration_status':'new image uncalibrated','phy_test_boundary':'actual DFI scheduler tested separately; vendor PHY calibration not simulated'}
(I/'profile.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
