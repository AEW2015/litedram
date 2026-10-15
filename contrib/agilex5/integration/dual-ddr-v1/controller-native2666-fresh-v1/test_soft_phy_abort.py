#!/usr/bin/env python3
"""End-to-end controller/soft-PHY read-return proof at native 333.333333 MHz.

Injects tagged 256-bit frames at the measured 18-cycle hard-PHY return age,
then checks the actual scheduler/frontend DFI receives the matching frame.
"""
import json, os, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'integration/dual-ddr-v1/controller-native2666-fresh-v1'))
from generate import make_controller
from migen import ClockDomain
from migen.sim import run_simulation, passive

ALIGNMENT=int(os.environ.get('READ_ALIGNMENT','0'))
PARTIAL_CAPTURE=bool(int(os.environ.get('PARTIAL_CAPTURE','1')))
REFRESH_WAIT=int(os.environ.get('REFRESH_WAIT','2700'))
BACK_TO_BACK=bool(int(os.environ.get('BACK_TO_BACK','0')))
assert 0 <= ALIGNMENT <= 4
BASE_READ_LATENCY=int(os.environ.get('BASE_READ_LATENCY','22'))
dut,_,_=make_controller(BASE_READ_LATENCY)
dut.clock_domains.cd_sys=ClockDomain('sys')
assert dut.phy.settings.read_latency == BASE_READ_LATENCY+2
assert dut.phy.settings.rdphase == 3 and dut.phy.settings.wrphase == 2
assert dut.phy.dfi is not dut.phy.backend_dfi
board=json.loads((ROOT/'config/board.json').read_text())
pins={p['signal']:p for p in board['pins']}
members=[]
for byte in range(4):
    members.append(sorted(range(byte*8,byte*8+8),
        key=lambda i:pins[f'io_ddr4_comp1_dq[{i}]']['io96']['io96_index']))

def frame_for(address):
    return sum((((address+1)*0x1020304+k*0x01010101)&0xffffffff) << (32*k)
               for k in range(8))

def phy_inputs(frame):
    result=[]
    for byte in range(4):
        word=0
        for sample in range(8):
            phase,half=sample//2,sample%2
            for pin,bit in enumerate(members[byte]):
                value=(frame>>(phase*64+half*32+bit))&1
                word |= value << (sample*8+pin)
        result.append(word)
    return result

target={64:frame_for(64),128:frame_for(128),512:frame_for(512)}
scheduled={}
partial_due={}
finished=[False]
read_req_edges=[]
frontend_req_edges=[]
refresh_events=[]
data_events=[]
current_cycle=[0]

@passive
def physical_return_model():
    cycle=0; previous_mask=0;previous_frontend=False
    while not finished[0]:
        return_now=scheduled.pop(cycle,None)
        request_mask=0
        for phase_i,p in enumerate(dut.phy.backend_dfi.phases):
            if (yield p.rddata_en): request_mask |= 1 << phase_i
        new_requests=request_mask & ~previous_mask
        frontend_request=False
        for p in dut.phy.dfi.phases:
            frontend_request |= (yield p.rddata_en)
        if frontend_request and not previous_frontend:
            frontend_req_edges.append({'cycle':cycle})
        previous_frontend=frontend_request
        for phase_i,p in enumerate(dut.phy.backend_dfi.phases):
            cs=(yield p.cs_n);ras=(yield p.ras_n);cas=(yield p.cas_n);we=(yield p.we_n)
            # DDR4 REF command: selected rank, RAS/CAS low and WE high.
            if cs==0 and ras==0 and cas==0 and we==1:
                refresh_events.append({'cycle':cycle,'phase':phase_i})
        if new_requests:
            assert new_requests & (new_requests-1) == 0, (cycle,hex(new_requests))
            assert cycle not in scheduled, (cycle,scheduled,hex(new_requests))
            # Firmware ages on the native 333.333333 MHz controller clock. At CL20,
            # rdphase3 and gate5 the fast bank0 MPR capture age was18.
            return_cycle=cycle+18
            address=(yield dut.phy.backend_dfi.phases[0].address)
            bank=(yield dut.phy.backend_dfi.phases[0].bank)
            tag=(bank<<24)|address
            # This fixture reads only bank zero and uses the known test address
            # sequence. The controller's DFI logical address can differ after
            # bank/column slicing, so bind returns to order rather than address.
            frame=next(iter(target.values()))
            idx=len(read_req_edges)
            if idx<len(target): frame=list(target.values())[idx]
            scheduled[return_cycle]=(idx,frame)
            read_req_edges.append({'cycle':cycle,'return_cycle':return_cycle,'tag':tag})
        previous_mask=request_mask
        for group in dut.phy.groups:
            yield group.valid.eq(0)
        if cycle in partial_due:
            values=partial_due.pop(cycle)
            for group,value in zip(dut.phy.groups,values):
                yield group.data_in.eq(value)
                yield group.valid.eq(12)
        if return_now is not None:
            idx,frame=return_now
            values=phy_inputs(frame)
            for group,value in zip(dut.phy.groups,values):
                yield group.data_in.eq(value)
                yield group.valid.eq(3 if PARTIAL_CAPTURE else 15)
            if PARTIAL_CAPTURE: partial_due[cycle+1]=values
            data_events.append({'cycle':cycle,'index':idx,'frame':frame})
        cycle+=1
        yield

@passive
def dfi_observer():
    cycle=0;previous_data=None;previous_valid=False
    previous_backend_valid=False;previous_physical_valid=False
    while not finished[0]:
        current_cycle[0]=cycle
        valid=False
        for p in dut.phy.dfi.phases:
            valid |= (yield p.rddata_valid)
        if valid:
            observed=0
            for i,p in enumerate(dut.phy.dfi.phases):
                observed |= (yield p.rddata) << (64*i)
            if valid and not previous_valid:
                dfi_observer.valid_edges.append({'cycle':cycle,'data':observed})
            if observed!=previous_data:
                dfi_observer.events.append({'cycle':cycle,'data':observed})
            previous_data=observed
        previous_valid=valid
        backend_valid=False
        for p in dut.phy.backend_dfi.phases:
            bit=(yield p.rddata_valid);backend_valid=backend_valid or bool(bit)
        if backend_valid and not previous_backend_valid:
            dfi_observer.backend_valid_edges.append({'cycle':cycle})
        previous_backend_valid=backend_valid
        physical_valid=False
        for p in dut.phy.physical_dfi.phases:
            bit=(yield p.rddata_valid);physical_valid=physical_valid or bool(bit)
        if physical_valid and not previous_physical_valid:
            dfi_observer.physical_valid_edges.append({'cycle':cycle})
        previous_physical_valid=physical_valid
        cycle+=1;yield
dfi_observer.events=[]
dfi_observer.valid_edges=[]
dfi_observer.backend_valid_edges=[]
dfi_observer.physical_valid_edges=[]

def wait(sig,what,limit=3000):
    for _ in range(limit):
        yield
        if (yield sig):return
    raise AssertionError(what+' timeout')

def csr_write(index,value):
    for _ in range(100):
        if not (yield dut.external_bus.waitrequest):break
        yield
    else:raise AssertionError('CSR wait timeout')
    yield dut.external_bus.address.eq(index*4)
    yield dut.external_bus.writedata.eq(value)
    yield dut.external_bus.write.eq(1);yield
    yield dut.external_bus.write.eq(0)
    for _ in range(3):yield

def bench():
    yield dut.phy.locked.eq(1)
    yield dut.phy.read_alignment.eq(ALIGNMENT)
    yield dut.phy.read_en_mask.eq(15)
    yield dut.phy.rd_delay.eq(5)
    yield dut.c.rdata_ready.eq(1)
    yield from csr_write(1,8)
    yield dut.c_request.eq(1)
    yield from wait(dut.c_granted,'native ownership')
    # Abort an admitted native read before its scheduled hard-PHY return.
    yield dut.c.cmd_address.eq(64);yield dut.c.cmd_valid.eq(1)
    yield from wait(dut.c.cmd_ready,'abort read admission')
    yield dut.c.cmd_valid.eq(0)
    while not read_req_edges: yield
    yield dut.cd_sys.rst.eq(1);yield dut.c_request.eq(0)
    scheduled.clear();partial_due.clear()
    for _ in range(8):yield
    assert not (yield dut.c_granted) and not (yield dut.c.rdata_valid)
    yield dut.cd_sys.rst.eq(0)
    for _ in range(100):yield
    assert not (yield dut.c.rdata_valid),'stale post-reset response'
    read_req_edges.clear();frontend_req_edges.clear();data_events.clear();refresh_events.clear()
    dfi_observer.events.clear();dfi_observer.valid_edges.clear()
    dfi_observer.backend_valid_edges.clear();dfi_observer.physical_valid_edges.clear()
    yield from csr_write(1,8)
    yield dut.c_request.eq(1)
    yield from wait(dut.c_granted,'post-reset native ownership')
    returned=[];host_cycles=[]
    addresses=[64,128,512]
    if BACK_TO_BACK:
        for address in addresses:
            yield dut.c.cmd_address.eq(address)
            yield dut.c.cmd_we.eq(0)
            yield dut.c.cmd_valid.eq(1)
            yield from wait(dut.c.cmd_ready,'queued read command')
            yield dut.c.cmd_valid.eq(0)
            yield
        for address in addresses:
            for _ in range(3000):
                yield
                if (yield dut.c.rdata_valid):break
            else:raise AssertionError('queued read return timeout')
            got=(yield dut.c.rdata_data);exp=target[address]
            assert got==exp,(address,hex(got),hex(exp),dfi_observer.events)
            returned.append({'address':address,'data':got})
            host_cycles.append(current_cycle[0])
            yield
    else:
      for address in addresses:
        yield dut.c.cmd_address.eq(address)
        yield dut.c.cmd_we.eq(0)
        yield dut.c.cmd_valid.eq(1)
        yield from wait(dut.c.cmd_ready,'read command')
        yield dut.c.cmd_valid.eq(0)
        for _ in range(3000):
            yield
            if (yield dut.c.rdata_valid):break
        else:raise AssertionError('read return timeout')
        got=(yield dut.c.rdata_data)
        exp=target[address]
        assert got==exp,(address,hex(got),hex(exp),dfi_observer.events)
        returned.append({'address':address,'data':got})
        host_cycles.append(current_cycle[0])
        # Allow the real scheduler to execute multiple refresh intervals
        # between tagged returns on the first iteration.
        if address==64:
            for _ in range(REFRESH_WAIT): yield
        yield
    # Drop and reacquire native ownership without resetting the PHY/controller.
    yield dut.c_request.eq(0)
    yield from wait(~dut.c_granted,'native ownership release')
    yield dut.c_request.eq(1)
    yield from wait(dut.c_granted,'native ownership reacquire')
    yield dut.c_drain_req.eq(1)
    yield from wait(dut.c_drain_done,'native drain')
    assert len(read_req_edges)==3,read_req_edges
    assert len(frontend_req_edges)==3,frontend_req_edges
    if REFRESH_WAIT>=2599:
        assert refresh_events,('expected at least one controller REFRESH during idle interval',refresh_events)
    assert len(data_events)==3,data_events
    # DFI's read-data-valid edge must qualify the matching payload. Merely
    # observing the payload later is insufficient for the LiteDRAM frontend.
    assert [event['data'] for event in dfi_observer.valid_edges] == list(target.values()), (
        'DFI valid/data misalignment', dfi_observer.valid_edges, dfi_observer.events)
    valid_cycles=[event['cycle'] for event in dfi_observer.valid_edges]
    capture_cycles=[x['cycle'] for x in data_events]
    assert all(v>=c+1 for v,c in zip(valid_cycles,capture_cycles)), (valid_cycles,capture_cycles)
    observed=[entry['data'] for entry in dfi_observer.events]
    pos=0
    for address in [64,128,512]:
        try:pos=observed.index(target[address],pos)+1
        except ValueError:raise AssertionError(('tagged DFI frame missing/out of order',address,dfi_observer.events))
    finished[0]=True
    print('NATIVE2666_FRESH_FULLPHY_PASS',json.dumps({
        'read_latency':dut.phy.settings.read_latency,'base_latency':BASE_READ_LATENCY,
        'read_alignment':ALIGNMENT,'partial_capture':PARTIAL_CAPTURE,'back_to_back':BACK_TO_BACK,
        'admitted_read_reset_abort':1,'read_age_cycles':18,'read_requests':read_req_edges,
        'frontend_dfi_requests':frontend_req_edges,
        'refresh_command_events':refresh_events,
        'hard_capture_events':[{k:v for k,v in x.items() if k!='frame'} for x in data_events],
        'dfi_valid_edges':dfi_observer.valid_edges,'backend_valid_edges':dfi_observer.backend_valid_edges,
        'physical_valid_edges':dfi_observer.physical_valid_edges,'dfi_valid_events':dfi_observer.events,'host_return_cycles':host_cycles,
        'returned':returned}))

run_simulation(dut.get_fragment(),{'sys':[bench(),physical_return_model(),dfi_observer()]},
               clocks={'sys':3000})
