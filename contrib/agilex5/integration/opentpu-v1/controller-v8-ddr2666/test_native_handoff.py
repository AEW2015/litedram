#!/usr/bin/env python3
"""Actual generated controller with a behavioral frontend DFI memory boundary.
The vendor PHY is removed, not simulated. All bank/core/stripe logic is actual.
"""
from generate_external_v8_ddr2666 import make_controller
from migen import ClockDomain
from migen.fhdl.specials import Memory
# Simulation time unit:1ps; period3000ticks =3ns, avoiding period//2 rounding.
from migen.sim import run_simulation, passive
# DFI rddata must be scheduled 23 yielded model steps after observed CAS:
# the configured latency is 24 cycles, and generator assignments settle on
# the following simulation edge. The modeled write-data offset is two cycles.

dut,_,_=make_controller()
dut.clock_domains.cd_sys=ClockDomain('sys')
dut.clock_domains.cd_observe=ClockDomain('observe')
dut._submodules=[(name,module) for name,module in dut._submodules if name!='phy']
# Inspect actual controller FIFO read/write ports. A valid synchronous read
# must never collide with an accepted write when no_rw_check is selected.
fragment=dut.get_fragment()
memories=[m for m in fragment.specials if isinstance(m,Memory)]
assert len(memories)==35
@passive
def check_fifo_collisions():
 while not finished[0]:
  if (yield dut.write_ingress.source.valid):
   assert not (yield dut.c_drain_done), 'drain while native write ingress occupied'
  for memory in memories:
   writers=[p for p in memory.ports if p.we is not None]
   readers=[p for p in memory.ports if p.we is None]
   for w in writers:
    if (yield w.we):
     for r in readers:
      assert not r.async_read, 'unexpected asynchronous controller FIFO'
      if r.re is not None and (yield r.re):
       assert (yield w.adr)!=(yield r.adr), 'valid FIFO read/write collision'
  yield
mem={};rows={};reads={};writes={};last_cas={};last_any_cas=[-100];act_at={};pre_at={};wr_at={};rd_at={};last_refresh=[-1000];last_write=[-1000];last_read=[-1000];finished=[False];count={'act':0,'read':0,'write':0,'refresh':0}
@passive
def model():
 cycle=0
 while not finished[0]:
  for p in dut.phy.dfi.phases:
   yield p.rddata_valid.eq(0)
  if cycle in reads:
   addr=reads.pop(cycle);data=mem.get(addr,0)
   for k,p in enumerate(dut.phy.dfi.phases):
    yield p.rddata.eq((data>>(k*64))&((1<<64)-1));yield p.rddata_valid.eq(1)
  if cycle in writes:
   addr=writes.pop(cycle);data=0
   for k,p in enumerate(dut.phy.dfi.phases): data|=(yield p.wrdata)<<(k*64)
   mem[addr]=data
  for p in dut.phy.dfi.phases:
   if not (yield p.cs_n):
    ras,cas,we=(yield p.ras_n),(yield p.cas_n),(yield p.we_n)
    bank,col=(yield p.bank),(yield p.address)
    if not ras and cas and we:
     assert cycle-pre_at.get(bank,-1000)>=7,'tRP violation'
     assert cycle-last_refresh[0]>=118,'tRFC violation'
     rows[bank]=col;act_at[bank]=cycle;count['act']+=1
    elif ras and not cas:
     assert bank in rows,'CAS without ACT'
     assert cycle-act_at[bank]>=7,'tRCD violation'
     assert cycle-last_any_cas[0]>=1,'tCCD_S violation'
     bg=bank>>2
     assert cycle-last_cas.get(bg,-100)>=2,'tCCD_L violation'
     last_cas[bg]=cycle;last_any_cas[0]=cycle
     addr=(rows[bank]<<11)|((bank&3)<<9)|((col>>3)<<2)|(bank>>2)
     if we:
      assert cycle-last_write[0]>=10,'write/read turnaround violation'
      last_read[0]=cycle;rd_at[bank]=cycle;reads[cycle+23]=addr;count['read']+=1
     else:
      assert cycle-last_read[0]>=24,'read/write turnaround violation'
      last_write[0]=cycle;wr_at[bank]=cycle;writes[cycle+2]=addr;count['write']+=1
    elif not ras and cas and not we:
     affected=range(16) if col & (1<<10) else [bank]
     for b in affected:
      assert cycle-wr_at.get(b,-1000)>=13,'write/precharge violation'
      assert cycle-rd_at.get(b,-1000)>=3,'read/precharge violation'
      pre_at[b]=cycle;rows.pop(b,None)
    elif not ras and not cas and we:
     assert not rows,'refresh with active bank'
     assert all(cycle-pre_at.get(b,-1000)>=7 for b in range(16)),'precharge/refresh tRP violation'
     last_refresh[0]=cycle;count['refresh']+=1;rows.clear()
  cycle+=1;yield

def wait_sig(sig,desc,limit=1500):
 for _ in range(limit):
  yield
  if (yield sig): return
 raise AssertionError(desc+' timeout')
def csr_write(index,value):
 for _ in range(50):
  if not (yield dut.external_bus.waitrequest):break
  yield
 else:raise AssertionError('native-handoff CSR wait timeout')
 yield dut.external_bus.address.eq(index*4);yield dut.external_bus.writedata.eq(value);yield dut.external_bus.write.eq(1)
 yield
 yield dut.external_bus.write.eq(0)
 for _ in range(5):yield
handoff={'grant_edge_checks':0,'acquisitions_with_dma_busy':0,'request_pending_dma_cycles':0,'request_pending_command_cycles':0,'request_pending_write_ingress_cycles':0,'request_pending_read_return_cycles':0,'withdrawn_write_grant_hold_cycles':0}
def bench():
 yield dut.phy.locked.eq(1);yield dut.c.rdata_ready.eq(1)
 yield from csr_write(1,8)
 for _ in range(12):yield
 for mode in [0,1]:
  for index,value in [(65,64),(66,4),(67,mode),(68,0x12345678),(69,4)]:
   yield from csr_write(index,value)
  # Start real diagnostic native traffic before requesting ownership.
  yield from csr_write(64,1)
  assert (yield dut.dma.busy), 'diagnostic transfer not active at request'
  handoff['acquisitions_with_dma_busy']+=1
  yield dut.c_request.eq(1)
  for _ in range(2000):
   if (yield dut.c_granted):break
   if (yield dut.dma.busy):handoff['request_pending_dma_cycles']+=1
   if (yield dut.dma_command_input.source.valid) or (yield dut.dma_command_buffer.nonempty):handoff['request_pending_command_cycles']+=1
   if (yield dut.write_ingress.source.valid):handoff['request_pending_write_ingress_cycles']+=1
   if (yield dut.dma.port.rdata.valid):handoff['request_pending_read_return_cycles']+=1
   yield
  else:raise AssertionError('native pending acquisition timeout')
  assert (yield dut.dma.done) and not (yield dut.dma.busy)
  assert (yield dut.dma.transferred)==4 and (yield dut.dma.errors)==0
  assert mem.get(64) is not None
  yield dut.c.cmd_address.eq(64);yield dut.c.cmd_we.eq(0);yield dut.c.cmd_valid.eq(1)
  yield from wait_sig(dut.c.cmd_ready,'handoff external read command');yield dut.c.cmd_valid.eq(0)
  yield from wait_sig(dut.c.rdata_valid,'handoff external read data')
  assert (yield dut.c.rdata_data)==mem[64]
  yield
  yield dut.c_drain_req.eq(1);yield from wait_sig(dut.c_drain_done,'handoff external read drain')
  yield dut.c_request.eq(0)
  for _ in range(5):yield
  assert not (yield dut.c_granted)
  yield dut.c_drain_req.eq(0)
 # Withdraw ownership after command acceptance and before write-data delivery.
 yield dut.c_request.eq(1);yield from wait_sig(dut.c_granted,'withdrawn-write ownership')
 yield dut.c.cmd_address.eq(128);yield dut.c.cmd_we.eq(1);yield dut.c.cmd_valid.eq(1)
 yield from wait_sig(dut.c.cmd_ready,'withdrawn-write command');yield dut.c.cmd_valid.eq(0)
 yield dut.c_request.eq(0);yield dut.c_drain_req.eq(1)
 for _ in range(4):
  yield
  assert (yield dut.c_granted), 'ownership relinquished with accepted write pending'
  handoff['withdrawn_write_grant_hold_cycles']+=1
 value=int('1234abcd'*8,16)
 yield dut.c.wdata_data.eq(value);yield dut.c.wdata_we.eq(0xffffffff);yield dut.c.wdata_valid.eq(1)
 yield from wait_sig(dut.c.wdata_ready,'withdrawn-write data');yield dut.c.wdata_valid.eq(0)
 for _ in range(2000):
  yield
  if not (yield dut.c_granted):break
 else:raise AssertionError('withdrawn-write final drain timeout')
 assert mem[128]==value
 assert handoff['grant_edge_checks']==3
 assert handoff['acquisitions_with_dma_busy']==2
 assert handoff['request_pending_dma_cycles']>0
 assert handoff['request_pending_command_cycles']>0
 assert handoff['request_pending_write_ingress_cycles']>0
 assert handoff['request_pending_read_return_cycles']>0
 assert handoff['withdrawn_write_grant_hold_cycles']==4
 finished[0]=True
 print('PASS actual controller native pending handoff and withdrawn-write drain',handoff,count)
 import hashlib,json
 from pathlib import Path
 result={'all_pass':True,'native_period_ps':3000,'simulation_tick_ps':1,'coverage':handoff,'dfi_transactions':count,'full_original_grant_predicate_checked':True}
 Path(__file__).with_name('native_handoff.json').write_text(json.dumps(result,indent=2)+'\n')
@passive
def check_grant_eligibility():
 previous=None
 while not finished[0]:
  granted=(yield dut.c_granted)
  if previous is not None and granted and not previous[0]:
   assert previous[1], 'DFI acquisition violates original full grant eligibility'
   handoff['grant_edge_checks']+=1
  previous=(granted,(yield dut.external_acquire_current_ok))
  yield
run_simulation(fragment,{'sys':[bench(),model(),check_fifo_collisions()],
                         'observe':check_grant_eligibility()},
               clocks={'sys':3000,'observe':(3000,500)})
