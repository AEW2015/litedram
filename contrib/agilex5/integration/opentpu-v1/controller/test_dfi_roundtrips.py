#!/usr/bin/env python3
"""Actual generated controller with a behavioral frontend DFI memory boundary.
The vendor PHY is removed, not simulated. All bank/core/stripe logic is actual.
"""
from generate_external import make_controller
from migen import ClockDomain
from migen.sim import run_simulation, passive
# DFI rddata must be scheduled 23 yielded model steps after observed CAS:
# the configured latency is 24 cycles, and generator assignments settle on
# the following simulation edge. The modeled write-data offset is two cycles.

dut,_,_=make_controller()
dut.clock_domains.cd_sys=ClockDomain('sys')
dut._submodules=[(name,module) for name,module in dut._submodules if name!='phy']
mem={};rows={};reads={};writes={};last_cas={};last_any_cas=[-100];finished=[False];count={'act':0,'read':0,'write':0,'refresh':0}
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
    if not ras and cas and we: rows[bank]=col;count['act']+=1
    elif ras and not cas:
     assert bank in rows,'CAS without ACT'
     assert cycle-last_any_cas[0]>=1,'tCCD_S violation'
     bg=bank>>2
     assert cycle-last_cas.get(bg,-100)>=2,'tCCD_L violation'
     last_cas[bg]=cycle;last_any_cas[0]=cycle
     addr=(rows[bank]<<11)|((bank&3)<<9)|((col>>3)<<2)|(bank>>2)
     if we: reads[cycle+23]=addr;count['read']+=1
     else: writes[cycle+2]=addr;count['write']+=1
    elif not ras and not cas and we: count['refresh']+=1;rows.clear()
  cycle+=1;yield

def wait_sig(sig,desc,limit=1500):
 for _ in range(limit):
  yield
  if (yield sig): return
 raise AssertionError(desc+' timeout')
def bench():
 yield dut.phy.locked.eq(1);yield dut.external_bus.address.eq(4);yield dut.external_bus.writedata.eq(8);yield dut.external_bus.write.eq(1);yield
 yield dut.external_bus.write.eq(0)
 for _ in range(12):yield
 yield dut.c.rdata_ready.eq(1);yield dut.c_request.eq(1)
 yield from wait_sig(dut.c_granted,'grant')
 for addr in [0,1,2,3,4,127,128,511,512,2048,0xfffffff]:
  val=sum((((addr+1)*0x1020304+k)&0xffffffff)<<(32*k) for k in range(8))
  yield dut.c.cmd_address.eq(addr);yield dut.c.cmd_we.eq(1);yield dut.c.cmd_valid.eq(1)
  yield from wait_sig(dut.c.cmd_ready,'write cmd');yield dut.c.cmd_valid.eq(0)
  yield dut.c.wdata_data.eq(val);yield dut.c.wdata_we.eq(0xffffffff);yield dut.c.wdata_valid.eq(1)
  yield from wait_sig(dut.c.wdata_ready,'write data');yield dut.c.wdata_valid.eq(0)
  yield dut.c_drain_req.eq(1);yield from wait_sig(dut.c_drain_done,'write drain');yield dut.c_drain_req.eq(0)
  assert mem.get(addr)==val,(addr,hex(mem.get(addr,0)),hex(val),list(mem.keys()))
  yield dut.c.cmd_we.eq(0);yield dut.c.cmd_valid.eq(1)
  yield from wait_sig(dut.c.cmd_ready,'read cmd');yield dut.c.cmd_valid.eq(0)
  yield from wait_sig(dut.c.rdata_valid,'read response')
  got=(yield dut.c.rdata_data);assert got==val,(addr,hex(got),hex(val))
  yield
 yield dut.c_drain_req.eq(1);yield from wait_sig(dut.c_drain_done,'final drain')
 finished[0]=True;print('PASS actual controller DFI round trips',count)
run_simulation(dut,[bench(),model()],clocks={'sys':10})
