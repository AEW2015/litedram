from pathlib import Path
import json,hashlib
N=Path(__file__).resolve().parent;R=N.parents[2]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
models=['model-latency25-full-a0.log','model-latency25-full-a1.log','model-latency25-partial-a0.log','model-optin-base23-partial-a1-queued.log','model-latency25-refresh.log','model-latency25-reset-abort.log','model-latency25-backpressure.log']
records={}
for name in models:
 p=N/name;lines=p.read_text().splitlines();line=next(x for x in lines if x.startswith('NATIVE2666_FRESH_FULLPHY_PASS '));d=json.loads(line.split(' ',1)[1]);assert d['base_latency']==23 and d['read_latency']==25 and len(d['returned'])==3
 records[name]={'sha256':sha(p),'result':d}
for name in ['frame-epochs-inflight-r2-a0.log','frame-epochs-inflight-r2-a1.log']:
 p=N/name;d=json.loads(p.read_text().split('FRAME_EPOCHS_PASS ',1)[1].splitlines()[0]);assert d['checked_frames']==14 and d['adjacent_full_frames']==8 and d['partial_clear']==1 and d['partial_reset']==1 and d['complete_inflight_reset']==1
 records[name]={'sha256':sha(p),'result':d}
for name,marker in [('sim-layered/test.log','PASS FRESH2666 layered controller differential'),('sim-layered/test-tagged.log','LAYERED_TAGGED_PASS requests=3 freshvalid=3 responses=3 heldcycles=15')]:
 p=N/name;assert marker in p.read_text();records[name]={'sha256':sha(p),'pass_marker':marker}
assert records[models[4]]['result']['refresh_command_events']
assert records[models[5]]['result']['admitted_read_reset_abort']==1
assert records[models[6]]['result']['response_hold_cycles_per_tag']==5
repro={}
for name in ['litedram_external_controller_raw.v','litedram_external_controller.v','generator_snapshot.py','repair_layers.json','profile.json']:
 a=N/'generated-latency25'/name;b=N/'generated-latency25-repro'/name;assert a.read_bytes()==b.read_bytes(),name;repro[name]=sha(a)
negative={}
for name in ['model-partial-a1.log','model-partial-a2.log']:
 p=N/name;assert 'AssertionError:' in p.read_text();negative[name]={'sha256':sha(p),'expected_failure':'fixed frontend24 deadline precedes aligned complete split frame'}
files=[p for p in N.rglob('*') if p.is_file() and (p.suffix in ['.py','.sv','.v','.json','.md']) and not any(x in p.parts for x in ['obj','obj-tagged','__pycache__']) and p.name!='prototype_proof.json']
source={str(p.relative_to(R)):sha(p) for p in files}
for name in ['config/board.json','litedram_phy/agilex5_fullrate_v22.py','litedram_phy/fullrate_dfi_output_pipeline_v2.py','integration/dual-ddr-v1/controller-native1600-v1/validity_patch.py','integration/dual-ddr-v1/controller-fast-v11/prepare.py','integration/dual-ddr-v1/controller-fast-v11/changes.json','build/dual_ddr_opentpu_v11/litedram_external_controller.v','integration/opentpu-v1/controller-v8-ddr2666/generate_external_v8_ddr2666.py']:
 p=R/name;source[name]=sha(p)
out={'prototype_only':True,'selected_hardware_seal':False,'synthesis_fit_hardware_runs':0,'tested_contract':{'base_latency':23,'frontend_latency':25,'CL':20,'CWL':14,'rdphase':3,'wrphase':2,'read_alignment':[0,1],'hard_capture_age_model_cycles':18,'split_lane_masks':[3,12],'native_clock_ps':3000},'all_required_prototype_checks_pass':True,'model_cases':7,'direct_phy_cases':2,'layered_verilog_cases':2,'records':records,'preserved_negative_evidence':negative,'reproducibility':repro,'source_sha256':source,'limits':['No analog or bank3B MPR remedy claimed','No arbitrary delayed lane arrival/deadline or alignments beyond0/1 qualified','No selected hardware successor or firmware profile created','Final layered tagged RTL tests use hard-group input model, not vendor analog PHY simulation']}
(N/'prototype_proof.json').write_text(json.dumps(out,indent=2)+'\n');print('PROTOTYPE_PROOF_PASS',len(source),'source pins')
