#!/usr/bin/env python3
"""Actual controller: original eligibility on grant edges, CSR tails and aborts.
Simulation time unit1ps: sys period3000ticks; observer samples500ps before edge.
"""
import hashlib,json
from pathlib import Path
from generate_external_v8_ddr2666 import make_controller
from migen import ClockDomain
from migen.sim import run_simulation,passive
D,source,_=make_controller()
D.clock_domains.cd_sys=ClockDomain('sys')
D.clock_domains.cd_observe=ClockDomain('observe')
finished=[False];arm=[None];aborted=[False]
counts={'grant_edges':0,'predicate_samples':0,'csr_busy_samples':0,'write_pending_samples':0,'request_drop_aborts':0,'calibration_aborts':0,'csr_race_cases':0,'blocked_start_write_cases':0}
@passive
def observer():
 prev=None
 while not finished[0]:
  g=(yield D.c_granted)
  if prev is not None and g and not prev['grant']:
   assert prev['eligible'],('grant violates original full eligibility',prev)
   assert prev['safe'] and prev['request'] and prev['ready']
   assert not prev['csr_busy'] and not prev['pending']
   counts['grant_edges']+=1
  if (yield D.csr_request_pipeline.busy):counts['csr_busy_samples']+=1
  if (yield D.csr_write_data_pipeline.pending):counts['write_pending_samples']+=1
  if arm[0] and (yield D.external_acquire_safe) and not g:
   if arm[0]=='drop':
    yield D.c_request.eq(0);counts['request_drop_aborts']+=1
   else:
    yield D.phy.locked.eq(0);counts['calibration_aborts']+=1
   arm[0]=None;aborted[0]=True
  prev={'grant':g,'eligible':(yield D.external_acquire_current_ok),'safe':(yield D.external_acquire_safe),'request':(yield D.c_request),'ready':(yield D.c_ready),'csr_busy':(yield D.csr_request_pipeline.busy),'pending':(yield D.csr_write_data_pipeline.pending)}
  counts['predicate_samples']+=1
  yield

def wait_high(signal,label,bound=200):
 for _ in range(bound):
  if (yield signal):return
  yield
 raise AssertionError(label+' timeout')
def idle_csr():
 for _ in range(30):
  if not (yield D.external_bus.waitrequest):return
  yield
 raise AssertionError('CSR tail timeout')
def pulse_write(address,data):
 yield from idle_csr()
 yield D.external_bus.address.eq(address);yield D.external_bus.writedata.eq(data);yield D.external_bus.write.eq(1)
 yield
 yield D.external_bus.write.eq(0)
def release():
 yield from wait_high(D.c_drain_done,'release drain')
 yield D.c_request.eq(0)
 for _ in range(6):yield
 assert not (yield D.c_granted),'ownership not released'

def bench():
 yield D.phy.locked.eq(1);yield D.c.rdata_ready.eq(1)
 yield from pulse_write(4,8)
 for _ in range(90):yield
 assert (yield D.c_ready)
 # Accepted write-data tails before, coincident with and after request rise.
 for offset in range(-3,4):
  if offset<0:
   yield from pulse_write(32,0x12345678+offset)
   for _ in range(-offset):yield
   yield D.c_request.eq(1)
  else:
   yield D.c_request.eq(1)
   for _ in range(offset):yield
   yield from pulse_write(32,0x12345678+offset)
  yield from wait_high(D.c_granted,'CSR race acquisition')
  counts['csr_race_cases']+=1
  yield from release()
 # Starts/control edits attempted while the request already inhibits diagnostics.
 for address,data in [(28,1),(28,2),(256,1),(4,0)]:
  yield D.c_request.eq(1)
  yield from pulse_write(address,data)
  yield from wait_high(D.c_granted,'blocked diagnostic acquisition')
  assert (yield D.c_ready),'blocked control edit changed calibration mode'
  counts['blocked_start_write_cases']+=1
  yield from release()
 # Abort between safe qualification and grant; independent observer drives
 # inputs500ps before the next native edge, rather than mutating DUT state.
 for mode in ['drop','calibration']:
  arm[0]=mode;aborted[0]=False
  yield D.c_request.eq(1)
  for _ in range(200):
   if aborted[0]:break
   yield
  else:raise AssertionError('abort qualification timeout')
  for _ in range(4):yield
  assert not (yield D.c_granted),'request/calibration abort leaked a grant'
  yield D.phy.locked.eq(1);yield D.c_request.eq(1)
  yield from wait_high(D.c_granted,'reacquire after abort')
  yield from release()
 for _ in range(3):yield
 assert counts['grant_edges']==13,counts
 assert counts['csr_busy_samples']>0 and counts['write_pending_samples']>0,counts
 assert counts['request_drop_aborts']==1 and counts['calibration_aborts']==1
 finished[0]=True
 print('PASS actual controller acquire eligibility CSR races abort calibration',counts)
 result={'all_pass':True,'native_period_ps':3000,'simulation_tick_ps':1,'observer_before_edge_ps':500,'counts':counts,'tested_transformed_source_sha256':hashlib.sha256(source.encode()).hexdigest()}
 Path(__file__).with_name('acquire_races.json').write_text(json.dumps(result,indent=2)+'\n')
run_simulation(D,{'sys':bench(),'observe':observer()},clocks={'sys':3000,'observe':(3000,500)})
