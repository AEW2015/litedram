from pathlib import Path
import json
from generate import make_controller
N=Path(__file__).resolve().parent;d,_,_=make_controller(23);g=d.dram.controller.settings.geom
assert (g.rowbits,g.colbits,g.bankbits)==(17,10,4)
assert len(d.c.cmd_address)==28 and d.phy.settings.nphases==4 and d.phy.settings.dfi_databits==64 and d.phy.settings.databits==32 and d.phy.settings.nranks==1
assert (1<<(17+10+4))*4==8589934592
out=N/'geometry-check.json';assert not out.exists();out.write_text(json.dumps({'geometry_verified_from_constructed_controller':True,'rowbits':g.rowbits,'colbits':g.colbits,'bankbits':g.bankbits,'native_address_bits':len(d.c.cmd_address),'native_data_bits':4*64,'PHY_bus_bits':32,'nphases':4,'ranks':1,'capacity_bytes':8589934592},indent=2)+'\n');print('GEOMETRY_CHECK_PASS')
