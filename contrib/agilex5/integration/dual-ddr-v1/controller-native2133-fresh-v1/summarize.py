from pathlib import Path
import json,hashlib
N=Path(__file__).resolve().parent;R=N.parents[2];h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
records={};negative={};matrix=[]
for age in [16,17,18]:
 for a in [0,1]:
  for split in [0,1]:
   p=N/f'model-base23-age{age}-a{a}-p{split}.log';s=p.read_text();passed='NATIVE2133_FRESH_FULLPHY_PASS ' in s
   matrix.append({'age':age,'alignment':a,'split':split,'pass':passed,'sha256':h(p)})
   if a==0 and age==16:assert not passed and 'AssertionError:' in s;negative[p.name]={'sha256':h(p),'reason':'alignment0 early arrival substitutes next queued tag'}
   else:
    assert passed,p;d=json.loads(s.split('NATIVE2133_FRESH_FULLPHY_PASS ',1)[1]);assert d['base_latency']==23 and d['read_latency']==25 and len(d['returned'])==3;records[p.name]={'sha256':h(p),'result':d}
for name in ['model-abort-r2-age16-a1.log','model-stall-age16-a1.log','model-refresh-age18-a1.log']:
 p=N/name;s=p.read_text();assert 'NATIVE2133_FRESH_FULLPHY_PASS ' in s;d=json.loads(s.split('NATIVE2133_FRESH_FULLPHY_PASS ',1)[1]);records[name]={'sha256':h(p),'result':d}
assert records['model-abort-r2-age16-a1.log']['result']['admitted_read_reset_abort']==1
assert records['model-stall-age16-a1.log']['result']['response_hold_cycles_per_tag']==5
assert records['model-refresh-age18-a1.log']['result']['refresh_command_events']
p=N/'frame-epochs-a1.log';d=json.loads(p.read_text().split('FRAME_EPOCHS_PASS ',1)[1]);assert d['checked_frames']==14 and d['complete_inflight_reset']==1;records[p.name]={'sha256':h(p),'result':d}
p=N/'write-phases-r4.log';d=json.loads(p.read_text().split('WRITE_PHASES_PASS ',1)[1]);assert d['CWL_pin_start_CK']==11 and d['native_writes']==3;records[p.name]={'sha256':h(p),'result':d}
for age in [16,17,18]:
 for split in [0,1]:
  p=N/f'sim-layered/age-anchored-age{age}-split{split}.log';s=p.read_text();marker=f'hard_age={age} split={split} actual_first={age} actual_last={age+split}';assert 'LAYERED_TAGGED_PASS' in s and marker in s,p;records[str(p.relative_to(N))]={'sha256':h(p),'verified_age_marker':marker}
for name,marker in [('sim-layered/test.log','PASS FRESH2133 layered controller differential'),('sim-layered/readphase-age18-split1.log','LAYERED_TAGGED_PASS')]:
 p=N/name;assert marker in p.read_text(),name;records[name]={'sha256':h(p),'pass_marker':marker}
for split in [0,1]:
 p=N/f'sim-layered/tagged-age16-split{split}.log';assert '%Fatal' in p.read_text();negative[str(p.relative_to(N))]={'sha256':h(p),'reason':'initial negedge fixture actually injects one-edge early; isolated diagnostic age15; source unchanged'}
p=N/'model-abort-age16-a1.log';assert 'AssertionError:' in p.read_text();negative[p.name]={'sha256':h(p),'reason':'reset replay omitted CSR78 profile restoration and ran alignment0; preserved failed setup'}
assert json.loads((N/'geometry-check.json').read_text())['geometry_verified_from_constructed_controller']
profile=json.loads((N/'generated-base23/profile.json').read_text());assert profile['base_read_latency']==23 and profile['frontend_read_latency']==25 and profile['rdphase']==3 and profile['wrphase']==1
for name in ['litedram_external_controller_raw.v','litedram_external_controller.v','generator_snapshot.py','profile.json']:
 assert (N/'generated-base23'/name).read_bytes()==(N/'generated-repro-r2'/name).read_bytes(),name
conditional={**profile,'required_read_alignment':1,'alignment_CSR_word_index':78,'alignment_must_be_programmed_before_native_and_after_global_reset':True,'modeled_first_fragment_diagnostic_ages':[16,17,18],'modeled_last_fragment_age_max':19,'consecutive_fragment_cycles_max':2,'profile_selection_by_root':False,'not_hardware_training':True,'controller_path':str((N/'generated-base23/litedram_external_controller.v').relative_to(R)),'controller_sha256':h(N/'generated-base23/litedram_external_controller.v')}
cp=N/'conditional_profile.json';assert not cp.exists();cp.write_text(json.dumps(conditional,indent=2)+'\n')
sources={str(p.relative_to(R)):h(p) for p in N.rglob('*') if p.is_file() and p.suffix in ['.py','.sv','.v','.json','.md'] and not any(part.startswith('obj') or part=='__pycache__' for part in p.parts) and p.name!='prototype_proof.json'}
proof={'prototype_only':True,'selected_hardware_seal':False,'profile_selection_by_root':False,'synthesis_fit_hardware_runs':0,'all_required_conditional_checks_pass':True,'all_initial_alignment_trials_pass':False,'required_read_alignment':1,'base_read_latency':23,'frontend_read_latency':25,'modeled_first_fragment_diagnostic_ages':[16,17,18],'controller_path':conditional['controller_path'],'controller_sha256':conditional['controller_sha256'],'conditional_profile_sha256':h(cp),'timing_cycles':profile['timing_cycles'],'native_period_ps':3750,'initial_matrix':matrix,'records':records,'negative_trials':negative,'source_sha256':sources,'generation_dependency_manifest_sha256':h(N/'generation-dependencies.json'),'reproduction_byte_identical':True,'limits':['No actual2133 PHY calibration or training yet','Alignment0 age16 fails; required profile alignment1 only','No arbitrary fragment latency/arrival window qualification','No Quartus/board/firmware execution performed','One-edge early Verilator negative is preserved and actual age anchoring asserted in final proof']}
p=N/'prototype_proof.json';assert not p.exists();p.write_text(json.dumps(proof,indent=2)+'\n');print('NATIVE2133_PROTOTYPE_PASS',h(p),conditional['controller_sha256'],len(records),len(sources))
