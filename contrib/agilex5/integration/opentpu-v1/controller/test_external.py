#!/usr/bin/env python3
"""Actual transformed Migen controller ownership smoke simulation."""
from generate_external import make_controller
from migen.sim import run_simulation
from migen import ClockDomain

dut,_,_=make_controller()
dut.clock_domains.cd_sys=ClockDomain('sys')
def bench():
    yield dut.phy.locked.eq(1)
    yield dut.external_bus.address.eq(4)
    yield dut.external_bus.writedata.eq(8)
    yield dut.external_bus.write.eq(1)
    yield
    yield dut.external_bus.write.eq(0)
    for _ in range(12): yield
    assert (yield dut.c_ready)==1
    yield dut.c.rdata_ready.eq(1)
    yield dut.c_request.eq(1)
    for _ in range(100): yield
    assert (yield dut.c_granted)==1, 'ownership grant timeout'
    assert (yield dut.c_drain_done)==1
    yield dut.c_drain_req.eq(1)
    yield dut.c.cmd_valid.eq(1)
    yield dut.c.cmd_we.eq(0)
    yield
    assert (yield dut.c.cmd_ready)==0, 'drain must block new commands'
    yield dut.c.cmd_valid.eq(0)
    yield dut.c_request.eq(0)
    for _ in range(4): yield
    assert (yield dut.c_granted)==0, 'drained ownership must release'
    print('PASS actual controller ownership/calibrated-ready/drain command inhibition/release')
run_simulation(dut,bench(),clocks={'sys':10})
