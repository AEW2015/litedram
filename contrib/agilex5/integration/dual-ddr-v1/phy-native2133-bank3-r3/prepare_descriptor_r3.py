#!/usr/bin/env python3
"""Copy the 2133 Qsys descriptor and swap only bank3 group0/group1 DQ lists."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT/'integration/dual-ddr-v1/phy-native2133-bank3-r2/generated/bank1/dual3_phy_probe_phy.ip'
DST = ROOT/'integration/dual-ddr-v1/phy-native2133-bank3-r3/source/dual3_phy_probe_phy.ip'
text = SRC.read_text()
updates = {
    'GUI_GROUP_0_DATA_PINS': ('48-51,56-59', '0-3,8-11'),
    'GUI_GROUP_1_DATA_PINS': ('0-3,8-11', '48-51,56-59'),
}
for key, (old, new) in updates.items():
    pat = re.compile(r'(<ipxact:parameter parameterId="'+re.escape(key)+r'".*?<ipxact:value>)'
                     +re.escape(old)+r'(</ipxact:value>.*?</ipxact:parameter>)', re.S)
    text, count = pat.subn(lambda m: m.group(1)+new+m.group(2), text)
    if count != 1: raise SystemExit(f'expected one {key} update, got {count}')
DST.parent.mkdir(parents=True, exist_ok=True)
DST.write_text(text)
print(f'Wrote {DST.relative_to(ROOT)} from {SRC.relative_to(ROOT)} with two group-list swaps')
