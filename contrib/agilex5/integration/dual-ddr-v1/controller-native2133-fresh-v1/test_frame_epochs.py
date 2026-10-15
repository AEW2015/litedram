#!/usr/bin/env python3
"""Direct physical DFI epoch proof, including adjacent complete frames."""
import json, os
from pathlib import Path
from migen import ClockDomain
from migen.sim import run_simulation, passive
from phy_native2133_fresh_v1 import Agilex5Native2133FreshPHY
ROOT=Path(__file__).resolve().parents[3]
ALIGN=int(os.environ.get('READ_ALIGNMENT','1'))
dut=Agilex5Native2133FreshPHY(read_latency=23)
dut.clock_domains.cd_sys=ClockDomain('sys')
pins={p['signal']:p for p in json.loads((ROOT/'config/board.json').read_text())['pins']}
members=[sorted(range(b*8,b*8+8),key=lambda i:pins[f'io_ddr4_comp1_dq[{i}]']['io96']['io96_index']) for b in range(4)]
def packed(frame):
 return [sum(((frame>>((s//2)*64+(s%2)*32+bit))&1)<<(s*8+p) for s in range(8) for p,bit in enumerate(bits)) for bits in members]
def tag(n): return sum((0x10203040+n*0x01010101+k)<<(32*k) for k in range(8))
expected=[]; observed=[]; done=[False]; epoch=[0]; drops=[0]
@passive
def observe():
 while not done[0]:
  valid=False
  for p in dut.physical_dfi.phases: valid |= bool((yield p.rddata_valid))
  if valid:
   value=0
   for i,p in enumerate(dut.physical_dfi.phases): value|=(yield p.rddata)<<(64*i)
   assert expected,('unsolicited valid',epoch[0],hex(value))
   want=expected.pop(0)
   assert value==want,('valid/payload mismatch',epoch[0],hex(value),hex(want))
   observed.append((epoch[0],value))
  yield

def drive(value,mask):
 for g,v in zip(dut.groups,packed(value)):
  yield g.data_in.eq(v);yield g.valid.eq(mask)
 yield

def idle(n=8):
 for g in dut.groups:yield g.valid.eq(0)
 for _ in range(n):yield

def clear():
 yield from idle(8)
 assert not expected,expected
 yield dut.clear_capture.eq(1);yield
 yield dut.clear_capture.eq(0);epoch[0]+=1
 yield from idle(8)

def bench():
 yield dut.read_alignment.eq(ALIGN)
 yield from idle(8)
 # Highest possible hard group throughput: one complete frame every native edge.
 for n in range(1,9):
  expected.append(tag(n));yield from drive(tag(n),15)
 yield from idle(10)
 assert not expected
 yield from clear()
 # Full256 payload assembled from two then four partial native masks.
 for n in range(9,12):
  yield from drive(tag(n),3)
  expected.append(tag(n));yield from drive(tag(n),12)
  yield from idle(4)
 for mask in [1,2,4]:yield from drive(tag(12),mask)
 expected.append(tag(12));yield from drive(tag(12),8)
 yield from idle(10)
 assert not expected
 # Clear must discard a partly assembled epoch, even if remaining lanes arrive.
 yield from drive(tag(99),3)
 yield dut.clear_capture.eq(1)
 for g in dut.groups:yield g.valid.eq(0)
 yield
 yield dut.clear_capture.eq(0);epoch[0]+=1
 yield from drive(tag(99),12)
 yield from idle(8)
 # Cancel the trailing partial mask before beginning a new complete frame.
 yield from clear()
 expected.append(tag(13));yield from drive(tag(13),15)
 yield from idle(10)
 assert not expected
 # Domain reset discards both partial assembly and queued valid history.
 yield from drive(tag(100),3)
 yield dut.cd_sys.rst.eq(1)
 for g in dut.groups:yield g.valid.eq(0)
 yield;yield
 yield dut.cd_sys.rst.eq(0);epoch[0]+=1
 yield from idle(8)
 expected.append(tag(14));yield from drive(tag(14),15)
 yield from idle(10)
 # Reset after a complete frame enters the validity pipeline, before output.
 yield from drive(tag(201),15)
 yield dut.cd_sys.rst.eq(1)
 for g in dut.groups:yield g.valid.eq(0)
 yield;yield
 yield dut.cd_sys.rst.eq(0);epoch[0]+=1
 yield from idle(8)
 assert len(observed)==14,(len(observed),observed)
 done[0]=True
 print('FRAME_EPOCHS_PASS',json.dumps({'alignment':ALIGN,'checked_frames':len(observed),'adjacent_full_frames':8,'split_frames':4,'partial_clear':1,'partial_reset':1,'complete_inflight_reset':1,'observed':observed}))
run_simulation(dut,{'sys':[bench(),observe()]},clocks={'sys':3750})
