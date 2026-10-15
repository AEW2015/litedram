#!/usr/bin/env python3
from pathlib import Path
import hashlib,json
I=Path(__file__).resolve().parent;R=I.parents[2];sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
checks={'ownership-native3.log':'PASS actual controller ownership','dfi.log':"PASS actual controller DFI round trips {'act': 9, 'read': 13, 'write': 11, 'refresh': 2}",'phy_commands.log':'PASS DDR2666 actual V22 command-frame fields 3584','lint.log':'Errors: 0, Warnings: 0','acquire_races.log':'PASS actual controller acquire eligibility CSR races abort calibration','native_handoff.log':'PASS actual controller native pending handoff and withdrawn-write drain'}
assert all(marker in (I/name).read_text() for name,marker in checks.items())
assert json.loads((I/'profile.json').read_text())['all_pass']
assert json.loads((I/'profile.json').read_text())['controller_rtl_sha256']==sha(I/'litedram_external_controller.v')
assert 'GRANT_PREDICATE_COVERAGE 1' in (I/'dfi.log').read_text()
acquire=json.loads((I/'acquire_races.json').read_text());handoff=json.loads((I/'native_handoff.json').read_text())
assert acquire['all_pass'] and acquire['native_period_ps']==3000 and acquire['simulation_tick_ps']==1 and acquire['counts']['grant_edges']==13
assert handoff['all_pass'] and handoff['native_period_ps']==3000 and handoff['simulation_tick_ps']==1 and handoff['coverage']['grant_edge_checks']==3
assert all(handoff['coverage'][name]>0 for name in ['request_pending_dma_cycles','request_pending_command_cycles','request_pending_write_ingress_cycles','request_pending_read_return_cycles','withdrawn_write_grant_hold_cycles'])
assert acquire['tested_transformed_source_sha256']==sha(I/'external_generator_snapshot.py')
assert all(f'PASS exact segmented timer {seed} unforced-cycle checks 15444' in (I/'timer_equivalence.log').read_text() for seed in [1,42,8675309])
old=I.parent/'controller-v5';assert sha(I/'fullrate_dma_v5.py')==sha(old/'fullrate_dma_v5.py')=='591ab6fc8276ad6fd05fac3b4645d4cc862438a3e6c6a7a0c8f84d44f9082d4c'
names=['generate_external_v8_ddr2666.py','fullrate_dma_v5.py','external_generator_snapshot.py','litedram_external_controller.v','test_external_v6_final.py','test_dfi_v6_final.py','test_phy_commands.py','profile_probe.py','profile.json','record_validation_final.py','segmented_timer_v8.py','test_timer_equivalence.py','timer_equivalence.log','test_acquire_races.py','acquire_races.json','test_native_handoff.py','native_handoff.json',*checks]
files=[I/name for name in names]+[old/'fullrate_dma_v5.py',old/'dma-equivalence.log',R/'build/dma-fullrate-2666-timing-v24/controller.json']
r={'all_pass':True,'native_period_ns':3,'simulation_tick_ps':1,'controller_clock_period_ticks':3000,'original_eligibility_grant_edges_checked':17,'acquire_races':acquire,'native_pending_handoff':handoff,'checks':checks,'dfi_transactions':{'act':9,'read':13,'write':11,'refresh':2},'fifo_collision_monitors':35,'phy_command_fields_checked':3584,'inherited_dma_equivalence':'byte-identical sealed V5 DMA source; historical copied V6 test_dma_equivalence.py not run','vendor_phy_calibration_simulated':False,'source_sha256':{str(p.relative_to(R)):sha(p) for p in files}}
(I/'validation.json').write_text(json.dumps(r,indent=2)+'\n');print('PASS V8 controller validation',len(files),'hashed files')
