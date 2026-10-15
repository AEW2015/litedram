#!/usr/bin/env python3
"""Actual V22 soft-PHY command-frame pipeline; excludes vendor primitives/DDR."""
from generate_external_v8_ddr2666 import make_controller
from migen import ClockDomain
from migen.genlib.record import DIR_M_TO_S
# Simulation time unit:1ps; period3000ticks =3ns, avoiding period//2 rounding.
from migen.sim import run_simulation
import random
controller,_,_=make_controller();phy=controller.phy
phy.clock_domains.cd_sys=ClockDomain('sys')
checks=[0]
def bench():
 rng=random.Random(1333)
 for _ in range(64):
  expected=[]
  for phase in phy.dfi.phases:
   fields={}
   for name,width,direction in phase.layout:
    if direction==DIR_M_TO_S:
     value=rng.randrange(1<<width);fields[name]=value
     yield getattr(phase,name).eq(value)
   expected.append(fields)
  # Keep each complete frame stable across capture and inspection. All fields
  # command/address/write-data/mask/request share the actual pipeline edge.
  yield
  yield
  for phase,fields in zip(phy.backend_dfi.phases,expected):
   for name,value in fields.items():
    assert (yield getattr(phase,name))==value,(name,value)
    checks[0]+=1
 yield
run_simulation(phy,bench(),clocks={'sys':3000})
print('PASS DDR2666 actual V22 command-frame fields',checks[0], 'CL20/CWL14 rdphase3/wrphase2 frontend_read24')
