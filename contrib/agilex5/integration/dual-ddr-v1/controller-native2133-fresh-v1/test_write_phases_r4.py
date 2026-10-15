"""Real native writes -> physical DFI commands and pin-group payload scoreboard."""
import json
from pathlib import Path
from generate import make_controller
from migen import ClockDomain
from migen.sim import run_simulation,passive
R=Path(__file__).resolve().parents[3]
dut,_,_=make_controller(23);dut.clock_domains.cd_sys=ClockDomain('sys')
assert dut.phy.settings.rdphase==3 and dut.phy.settings.wrphase==1
pins={p['signal']:p for p in json.loads((R/'config/board.json').read_text())['pins']}
members=[sorted(range(b*8,b*8+8),key=lambda i:pins[f'io_ddr4_comp1_dq[{i}]']['io96']['io96_index']) for b in range(4)]
tags=[sum((0x10203040+n*0x01010101+k)<<(32*k) for k in range(8)) for n in range(3)]
def decode(values):
 return sum(((values[b]>>(s*8+p))&1)<<((s//2)*64+(s%2)*32+bit) for b,bits in enumerate(members) for s in range(8) for p,bit in enumerate(bits))
command_events=[];payload_events=[];done=[False]
@passive
def observe():
 cycle=0
 while not done[0]:
  for phase,p in enumerate(dut.phy.physical_dfi.phases):
   if (yield p.cs_n)==0 and (yield p.ras_n)==1 and (yield p.cas_n)==0 and (yield p.we_n)==0:
    command_events.append({'cycle':cycle,'phase':phase,'bank':(yield p.bank),'address':(yield p.address)})
  oe=[(yield dut.phy.groups[0].oe),(yield dut.phy.groups[1].oe),(yield dut.phy.groups[2].oe),(yield dut.phy.groups[3].oe)]
  if any(oe):
   assert oe==[15]*4,oe
   values=[]
   for g in dut.phy.groups:values.append((yield g.data_out))
   frame=decode(values);idx=len(payload_events);assert idx<3 and frame==tags[idx],('write payload phase/tag',idx,hex(frame),list(map(hex,tags)))
   payload_events.append({'cycle':cycle,'frame':frame})
  cycle+=1;yield

def wait(sig,limit=3000):
 for _ in range(limit):
  yield
  if (yield sig):return
 raise AssertionError('timeout')

def bench():
 yield dut.phy.locked.eq(1);yield dut.phy.wr_delay.eq(2)
 yield dut.external_bus.address.eq(4);yield dut.external_bus.writedata.eq(8);yield dut.external_bus.write.eq(1);yield
 yield dut.external_bus.write.eq(0)
 for _ in range(10):yield
 yield dut.c_request.eq(1);yield from wait(dut.c_granted)
 for idx,t in enumerate(tags):
  yield dut.c.cmd_address.eq(64+idx*32);yield dut.c.cmd_we.eq(1);yield dut.c.cmd_valid.eq(1)
  yield dut.c.wdata_data.eq(t);yield dut.c.wdata_we.eq(0xffffffff);yield dut.c.wdata_valid.eq(1)
  ca=wa=False
  for _ in range(3000):
   yield
   if not ca and (yield dut.c.cmd_ready):ca=True;yield dut.c.cmd_valid.eq(0)
   if not wa and (yield dut.c.wdata_ready):wa=True;yield dut.c.wdata_valid.eq(0)
   if ca and wa:break
  assert ca and wa
  yield
 yield dut.c_drain_req.eq(1);yield from wait(dut.c_drain_done)
 assert len(command_events)==len(payload_events)==3,(command_events,payload_events)
 assert all(e['phase']==1 for e in command_events),command_events
 assert all(4*(b['cycle']-a['cycle'])-a['phase']==11 for a,b in zip(command_events,payload_events)),('CWL pin-start contract',command_events,payload_events)
 done[0]=True
 print('WRITE_PHASES_PASS',json.dumps({'CL':16,'CWL':11,'rdphase':3,'wrphase':1,'CWL_pin_start_CK':11,'native_writes':3,'pin_payload_tags':3,'physical_commands':command_events,'payload_events':payload_events}))
run_simulation(dut.get_fragment(),{'sys':[bench(),observe()]},clocks={'sys':3750})
