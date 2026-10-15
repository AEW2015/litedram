#!/usr/bin/env python3
"""Actual timer equivalence: one injected initial state, then unforced RTL steps."""
import generate_external_v8_ddr2666
from segmented_timer_v8 import SegmentedRefreshTimer
from litedram.core.refresher import RefreshTimer
from migen import *
from migen.sim import run_simulation
import random
class Pair(Module):
 def __init__(self):
  self.clock_domains.cd_sys=ClockDomain('sys')
  self.submodules.ref=RefreshTimer(333333333)
  self.submodules.new=SegmentedRefreshTimer(333333333)
  self.ref_count=self.ref._fragment.comb[-1].r
for seed in [1,42,8675309]:
 d=Pair();r=random.Random(seed);checks=[0]
 def assert_state():
  assert (yield d.ref.count)==(yield d.new.count)
  assert (yield d.ref.done)==(yield d.new.done)
  count=(yield d.new.count)
  for lo,flag in d.new.prefix_zero.items():
   assert (yield flag)==((count & ((1<<lo)-1))==0)
  checks[0]+=1
 def initialize_scenario(value):
  # Inject equivalent reachable-register states once per scenario. This is
  # the ONLY write to cached flags; all later steps are DUT-generated.
  yield d.cd_sys.rst.eq(0)
  yield d.ref.wait.eq(1);yield d.new.wait.eq(1)
  yield d.ref_count.eq(value);yield d.new.count.eq(value)
  for lo,flag in d.new.prefix_zero.items():
   yield flag.eq((value & ((1<<lo)-1))==0)
  yield
  yield from assert_state()
 def step(wait,reset):
  # Read/check the cache; never assign a count or prefix flag here.
  yield d.ref.wait.eq(wait);yield d.new.wait.eq(wait)
  yield d.cd_sys.rst.eq(reset)
  yield
  yield from assert_state()
 def bench():
  boundaries=[0,1,2,333333332]+[max(0,(1<<bit)+delta) for bit in [6,12,18,24,28] for delta in [-2,-1,0,1,2]]
  for value in boundaries+[r.randrange(333333333) for _ in range(400)]:
   yield from initialize_scenario(value)
   for _ in range(10):yield from step(1,0) # Cross each nearby borrow/expiration.
   yield from step(0,0)                   # Reload without waiting.
   for _ in range(10):yield from step(1,0)
   yield from step(1,1)                   # Global reset during active countdown.
   yield from step(0,1)
   for _ in range(12):yield from step(r.randrange(2),0)
 run_simulation(d,bench(),clocks={'sys':3})
 print('PASS exact segmented timer',seed,'unforced-cycle checks',checks[0])
