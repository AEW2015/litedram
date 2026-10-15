#!/usr/bin/env python3
"""Namespace freshly Qsys-generated 2133 bank3 PHY and prove its map."""
from __future__ import annotations
import hashlib, json, re, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = ROOT / 'integration/dual-ddr-v1/phy-native2133-bank3-r3'
RAW = HERE / 'source/dual3_phy_probe_phy'
DONOR = ROOT / 'integration/dual-ddr-v1/phy-native2133-bank3-r2/generated/bank1/dual3_phy_probe_phy'
DONOR_IP = DONOR.parent / 'dual3_phy_probe_phy.ip'
BANK0 = ROOT / 'integration/dual-ddr-v1/phy-native2133-v1/generated/bank0/phy_probe_phy'
OUT = HERE / 'generated/bank1/dual3_phy_probe_phy'

def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def modules(root: Path) -> set[str]:
    out = set()
    for p in root.rglob('*'):
        if p.suffix.lower() not in {'.v', '.sv'}: continue
        try: s = p.read_text()
        except UnicodeDecodeError: continue
        s = re.sub(r'/\*.*?\*/|//[^\n]*', '', s, flags=re.S)
        out.update(re.findall(r'(?m)^\s*module\s+([A-Za-z_]\w*)', s))
    return out

def procedures(root: Path) -> set[str]:
    out = set()
    for p in root.rglob('*'):
        if p.suffix.lower() not in {'.tcl', '.sdc'}: continue
        s = p.read_text(errors='ignore')
        out.update(re.findall(r'(?m)^\s*proc\s+([A-Za-z_]\w*)', s))
    return out

if not RAW.is_dir() or not DONOR.is_dir() or not BANK0.is_dir():
    raise SystemExit('missing raw generation or comparison tree')
if not DONOR_IP.is_file(): raise SystemExit('missing original descriptor')
if OUT.exists():
    raise SystemExit(f'refusing to overwrite existing output: {OUT}')

def gui_values(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'<ipxact:parameter parameterId="([^"]+)".*?</ipxact:parameter>',
                         text, re.S):
        vals = re.findall(r'<ipxact:value>(.*?)</ipxact:value>', m.group(0), re.S)
        if vals: out[m.group(1)] = vals[-1].strip()
    return out

base_ip = DONOR_IP.read_text()
source_ip = (HERE/'source/dual3_phy_probe_phy.ip').read_text()
base_values, source_values = gui_values(base_ip), gui_values(source_ip)
gui_diff = {k for k in base_values.keys() | source_values.keys()
            if base_values.get(k) != source_values.get(k)}
if gui_diff != {'GUI_GROUP_0_DATA_PINS', 'GUI_GROUP_1_DATA_PINS'}:
    raise SystemExit(f'descriptor changes beyond the two approved group pin lists: {sorted(gui_diff)}')
if (source_values['GUI_GROUP_0_DATA_PINS'], source_values['GUI_GROUP_1_DATA_PINS']) != (
        '0-3,8-11', '48-51,56-59'):
    raise SystemExit('descriptor bank3 group pin assignment is unexpected')

for fresh_name, donor_name in (('dual3_phy_probe_phy_bb.v', 'phy_probe_phy_bb.v'),
                               ('dual3_phy_probe_phy_inst.v', 'phy_probe_phy_inst.v')):
    fresh = RAW/fresh_name
    donor = DONOR/donor_name
    if not fresh.is_file() or not donor.is_file() or fresh.read_bytes() != donor.read_bytes():
        raise SystemExit(f'component port/interface wrapper differs: {fresh_name}')
fresh_top = (RAW/'synth/dual3_phy_probe_phy.v').read_text()
donor_top = (DONOR/'synth/dual3_phy_probe_phy.v').read_text()
fresh_top = re.sub(r'_phylite_ph2_941_[A-Za-z0-9]+(?=\s+phy\s*\()', '_phylite_ph2_941_HASH', fresh_top)
donor_top = re.sub(r'_phylite_ph2_941_[A-Za-z0-9]+(?=\s+phy\s*\()', '_phylite_ph2_941_HASH', donor_top)
if fresh_top != donor_top:
    raise SystemExit('component top differs beyond regenerated PH2 module identity')

fresh_arch, donor_arch = RAW/'phylite_arch_fp_0/synth', DONOR/'dual3_arch/synth'
def bank1_names(text: str) -> str:
    for name in sorted(modules(BANK0), key=len, reverse=True):
        if not name.startswith('dual3_'):
            text = re.sub(r'\b'+re.escape(name)+r'\b', 'dual3_'+name, text)
    return text
for fresh_name, donor_name in (
        ('phylite_iobufs_modified.sv','phylite_iobufs_modified.sv'),
        ('phylite_lane_modified.sv','phylite_lane_modified.sv'),
        ('phylite_byte_pin_param_mappings.vh','phylite_byte_pin_param_mappings.vh'),
        ('phylite_c2p_p2c_mapping.sv','dual3_phylite_c2p_p2c_mapping.sv'),
        ('phylite_clocking.sv','dual3_phylite_clocking.sv'),
        ('phylite_hipi_regs.sv','dual3_phylite_hipi_regs.sv')):
    if bank1_names((fresh_arch/fresh_name).read_text()) != (donor_arch/donor_name).read_text():
        raise SystemExit(f'functional PHY helper differs beyond map metadata: {fresh_name}')
fresh_top_src = bank1_names((fresh_arch/'d_phylite_top_modified.sv').read_text().replace(
    'd_phylite_top', 'p_phylite_top'))
donor_top_src = (donor_arch/'p_phylite_top_modified.sv').read_text()
if fresh_top_src != donor_top_src:
    raise SystemExit('generated PHY top logic differs beyond module name')

raw_modules, bank0_modules = modules(RAW), modules(BANK0)
raw_procs, bank0_procs = procedures(RAW), procedures(BANK0)
# Every generic helper is bank-private. The generated component-root/PHY module
# identities already contain dual3 and remain unique. Mathematical Tcl helpers
# are pure and stay shared, matching prior qualified clone practice.
rename = {x: 'dual3_' + x for x in raw_modules & bank0_modules
          if not x.startswith('dual3_')}
rename.update({x: 'dual3_' + x for x in raw_procs & bank0_procs
               if x not in {'min', 'max'}})
rename['phylite_arch_fp_0'] = 'dual3_arch'
rename['phylite_ph2_941'] = 'dual3_ph2'
rename['d_phylite_top'] = 'dual3_d_phylite_top'
parts = [r'\b' + re.escape(x) + r'\b' for x in sorted(rename, key=len, reverse=True)]
pattern = re.compile('|'.join(parts))
def transform(s: str) -> str:
    s = pattern.sub(lambda m: rename[m[0]], s)
    # Vendor caller names otherwise collide at timing.sdc lines 397/406.
    s, n = re.subn(r'(-name\s+")group_\$\{i\}_lane_\$\{n\}_dqsn_nff_clk("\s+\\)',
                   r'\1${inst}_group_${i}_lane_${n}_dqsn_nff_clk\2', s)
    # The pattern may have a trailing backslash on the following line; assert
    # exactly both generated-clock declarations were scoped.
    if n not in {0, 2}:
        raise SystemExit(f'unexpected count while namespacing generated clocks: {n}')
    return s

OUT.mkdir(parents=True)
for p in RAW.rglob('*'):
    rel = p.relative_to(RAW)
    rel_s = pattern.sub(lambda m: rename[m[0]], str(rel))
    q = OUT / rel_s
    if p.is_dir():
        q.mkdir(parents=True, exist_ok=True)
        continue
    q.parent.mkdir(parents=True, exist_ok=True)
    try: q.write_text(transform(p.read_text()))
    except UnicodeDecodeError: shutil.copy2(p, q)

# The component QIP references its source .ip as ../dual3_phy_probe_phy.ip.
shutil.copy2(HERE/'source/dual3_phy_probe_phy.ip', OUT.parent/'dual3_phy_probe_phy.ip')

def get_maps(root: Path):
    p = next(root.rglob('*ph2*.v'))
    s = p.read_text()
    return {f: {int(i): int(v) for i,v in re.findall(
        r'\.MAP_INDEX_TO_' + f + r'_(\d+)\s*\(\s*(-?\d+)\s*\)', s)}
        for f in ('GROUP', 'PIN_DATA', 'PIN_STROBE')}, p

new, new_ph2 = get_maps(OUT)
donor, donor_ph2 = get_maps(DONOR)
if any(len(new[f]) != 96 or len(donor[f]) != 96 for f in new):
    raise SystemExit('expected all 96 PH2 mapping entries')
if new != donor:
    raise SystemExit('freshly regenerated 96 PH2 map differs from corrected V4 source')
fresh_ph2_text, donor_ph2_text = new_ph2.read_text(), donor_ph2.read_text()
for oldid, newid in (('_ojudnri','_HASH'),('_kx4vsfq','_HASH'),
                     ('_m4zwqqa','_ARCHHASH'),('_qp7sb5i','_ARCHHASH')):
    fresh_ph2_text = fresh_ph2_text.replace(oldid, newid)
    donor_ph2_text = donor_ph2_text.replace(oldid, newid)
if fresh_ph2_text != donor_ph2_text:
    raise SystemExit('PH2 source differs beyond regenerated module identities')
raw_meta = next(RAW.rglob('*timing_ip_parameters.tcl')).read_text()
donor_meta = next(DONOR.rglob('*timing_ip_parameters.tcl')).read_text()
def tcl_vars(text: str) -> dict[str, str]:
    return dict(re.findall(r'^set var\(([^)]+)\)\s+(.*)$', text, re.M))
raw_vars, donor_vars = tcl_vars(raw_meta), tcl_vars(donor_meta)
meta_diff = {k for k in raw_vars.keys() | donor_vars.keys()
             if raw_vars.get(k) != donor_vars.get(k)}
expected_diff = ({f'MAP_INDEX_TO_GROUP_{i}' for i in range(12)} |
                 {f'MAP_INDEX_TO_GROUP_{i}' for i in range(48, 60)} |
                 {'GROUP_0_START_LANE_ID', 'GROUP_1_START_LANE_ID'} |
                 {f'PATTERNS_{kind},{g}' for kind in
                  ('RCLK','RDATA','WDATA','WCLK','WCLK_N') for g in (0,1)})
if meta_diff != expected_diff:
    raise SystemExit(f'unexpected timing metadata differences: {sorted(meta_diff ^ expected_diff)}')
if any('3752 ps' not in raw_vars.get(k,'') for k in ('PLL_REF_CLK_FREQ_PS_STR',)):
    raise SystemExit('unexpected PHY reference clock setting')
if any(raw_vars.get(k) != donor_vars.get(k) for k in
       ('PLL_REF_CLK_FREQ_PS_STR','PLL_M_DIV','PLL_N_DIV','PHY_DDR_RATE','PHY_CLK_FREQ_PS_STR')):
    raise SystemExit('non-mapping clock parameter differs from corrected V4 source')
if OUT == DONOR:
    raise SystemExit('invalid donor/output alias')
if modules(OUT) & bank0_modules:
    raise SystemExit(f'module namespace collision: {sorted(modules(OUT) & bank0_modules)}')
if procedures(OUT) & bank0_procs - {'min', 'max'}:
    raise SystemExit(f'Tcl procedure namespace collision: {sorted(procedures(OUT) & bank0_procs - {"min", "max"})}')

qip = OUT / 'dual3_phy_probe_phy.qip'
if not qip.is_file(): raise SystemExit('missing component QIP')
missing = []
for line in qip.read_text().splitlines():
    m = re.search(r'\[file join \$::quartus\(qip_path\) "([^"]+)"\]', line)
    if m and not (OUT / m[1]).resolve().exists(): missing.append(m[1])
if missing: raise SystemExit(f'QIP closure has missing files: {missing}')
def qip_libraries(path: Path) -> set[str]:
    return set(re.findall(r'-library "([^"]+)"', path.read_text()))
bank0_libraries = qip_libraries(BANK0/'phy_probe_phy.qip')
bank1_libraries = qip_libraries(qip)
if bank0_libraries & bank1_libraries:
    raise SystemExit(f'QIP library namespace collision: {sorted(bank0_libraries & bank1_libraries)}')

record = {
    'schema': 'native2133-bank3-qsys-r3-map-and-namespace-proof-v1',
    'status': 'PASS_QSYS_GENERATION_MAP_NAMESPACE_QIP_CLOSURE',
    'descriptor': str((HERE/'source/dual3_phy_probe_phy.ip').relative_to(ROOT)),
    'descriptor_sha256': sha(HERE/'source/dual3_phy_probe_phy.ip'),
    'descriptor_only_changes_vs_v4': sorted(gui_diff),
    'descriptor_group_data_pins': {
        'group0': source_values['GUI_GROUP_0_DATA_PINS'],
        'group1': source_values['GUI_GROUP_1_DATA_PINS']},
    'component_blackbox_and_instantiation_interfaces_byte_identical_to_v4': True,
    'top_wrapper_diff_only_regenerated_ph2_identity': True,
    'functional_phy_architecture_helpers_equal_v4_after_module_rename': True,
    'ph2_functional_source_equal_v4_after_ip_hash_rename': True,
    'qsys_log': str((HERE/'qsys-generate.log').relative_to(ROOT)),
    'raw_tree': str(RAW.relative_to(ROOT)),
    'raw_ph2': str(next(RAW.rglob('*ph2*.v')).relative_to(ROOT)),
    'raw_ph2_sha256': sha(next(RAW.rglob('*ph2*.v'))),
    'corrected_v4_donor_tree': str(DONOR.relative_to(ROOT)),
    'donor_ph2': str(donor_ph2.relative_to(ROOT)),
    'donor_ph2_sha256': sha(donor_ph2),
    'output_tree': str(OUT.relative_to(ROOT)),
    'output_ph2': str(new_ph2.relative_to(ROOT)),
    'output_ph2_sha256': sha(new_ph2),
    'all_96_group_data_strobe_tuples_equal_corrected_v4': True,
    'per_map_counts': {k: len(v) for k,v in new.items()},
    'bank0_module_collisions': [],
    'bank0_tcl_procedure_collisions_except_min_max': [],
    'qip_relative_file_references_exist': True,
    'qip_file_reference_count': len(re.findall(r'\[file join \$::quartus\(qip_path\)', qip.read_text())),
    'bank0_qip_libraries': sorted(bank0_libraries),
    'bank1_qip_libraries': sorted(bank1_libraries),
    'qip_library_namespace_disjoint': True,
    'generated_timing_profile': {
        k: raw_vars[k] for k in ('PHY_REF_CLK_FREQ_MHZ','PHY_MEM_CLK_FREQ_MHZ',
                                 'PLL_REF_CLK_FREQ_PS_STR','PLL_VCO_FREQ_PS_STR',
                                 'PLL_M_DIV','PLL_N_DIV')},
    'namespaced_dqsn_clock_caller_count': sum(len(re.findall(
        r'-name "\$\{inst\}_group_\$\{i\}_lane_\$\{n\}_dqsn_nff_clk"',
        p.read_text(errors='ignore'))) for p in OUT.rglob('*.sdc')),
    'timing_metadata_difference_count_vs_v4': len(meta_diff),
    'timing_metadata_difference_names_vs_v4': sorted(meta_diff),
    'non_mapping_clock_parameters_equal_v4': True,
    'full_quartus_compile_performed': False,
    'hardware_touched': False,
    'qualification': False,
}
if record['namespaced_dqsn_clock_caller_count'] != 2:
    raise SystemExit('expected exactly two per-instance DQS clock declarations')
proof = HERE/'qsys-r3-map-proof.json'
proof.write_text(json.dumps(record, indent=2)+'\n')
print(f'PASS: regenerated PHY map (96x3), namespace, QIP closure; proof {proof}')
