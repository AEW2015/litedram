#!/usr/bin/env python3
"""Namespace fresh bank3 PHYLite output; no generation, donor tree or hardware.

Requires separately Qsys-generated bank0 and bank3 components. For the selected
x32 bank3 mapping descriptor group0/group1 are 0-3,8-11 / 48-51,56-59.
The output is a new component directory; its .ip descriptor is copied beside it.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def declarations(root, kind):
    result = set()
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        if kind == 'module' and path.suffix in ('.v', '.sv'):
            text = re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_text(), flags=re.S)
            result.update(re.findall(r'(?m)^\s*module\s+([A-Za-z_]\w*)', text))
        elif kind == 'proc' and path.suffix in ('.tcl', '.sdc'):
            result.update(re.findall(r'(?m)^\s*proc\s+([A-Za-z_]\w*)', path.read_text()))
    return result


def maps(root):
    paths = list(root.rglob('*ph2*.v'))
    assert len(paths) == 1, 'Expected one PH2 Verilog file'
    text = paths[0].read_text()
    result = {field: dict(re.findall(r'\.MAP_INDEX_TO_' + field +
              r'_(\d+)\s*\(\s*(-?\d+)\s*\)', text))
              for field in ('GROUP', 'PIN_DATA', 'PIN_STROBE')}
    assert all(len(values) == 96 for values in result.values()), 'Incomplete PH2 map'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bank0', type=Path, required=True)
    parser.add_argument('--bank3-raw', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--descriptor', type=Path,
                        help='Bank3 .ip; otherwise use adjacent component-name.ip when present')
    args = parser.parse_args()
    bank0, raw, output = (p.resolve() for p in (args.bank0, args.bank3_raw, args.output))
    assert bank0.is_dir() and raw.is_dir(), 'Missing generated component tree'
    assert not output.exists(), 'Output must be a new directory'
    assert not output.is_relative_to(raw) and not output.is_relative_to(bank0), 'Output aliases input'
    descriptor = args.descriptor or raw.parent / (raw.name + '.ip')
    descriptor = descriptor.resolve() if descriptor.is_file() else None
    if args.descriptor:
        assert descriptor, 'Missing supplied descriptor'
    if descriptor:
        values = {}
        for block in re.findall(r'<ipxact:parameter parameterId="[^\"]+".*?</ipxact:parameter>',
                                descriptor.read_text(), re.S):
            key = re.search(r'parameterId="([^\"]+)"', block)[1]
            matches = re.findall(r'<ipxact:value>(.*?)</ipxact:value>', block, re.S)
            if matches:
                values[key] = matches[-1].strip()
        assert [values.get('GUI_GROUP_0_DATA_PINS'), values.get('GUI_GROUP_1_DATA_PINS')] == [
            '0-3,8-11', '48-51,56-59'], 'Unexpected bank3 group0/group1 pin lists'
        target_descriptor = output.parent / descriptor.name
        assert not target_descriptor.exists() or sha(target_descriptor) == sha(descriptor), 'Descriptor collision'
    b0mods, rawmods = declarations(bank0, 'module'), declarations(raw, 'module')
    b0procs, rawprocs = declarations(bank0, 'proc'), declarations(raw, 'proc')
    rename = {name: 'dual3_' + name for name in rawmods & b0mods if not name.startswith('dual3_')}
    rename.update({name: 'dual3_' + name for name in rawprocs & b0procs if name not in {'min', 'max'}})
    rename.update({'phylite_arch_fp_0': 'dual3_arch', 'phylite_ph2_941': 'dual3_ph2',
                   'd_phylite_top': 'dual3_d_phylite_top'})
    pattern = re.compile('|'.join(r'\b' + re.escape(name) + r'\b'
                                  for name in sorted(rename, key=len, reverse=True)))
    def transform(text):
        text = pattern.sub(lambda match: rename[match[0]], text)
        text, count = re.subn(r'(-name\s+")group_\$\{i\}_lane_\$\{n\}_dqsn_nff_clk("\s+\\)',
                              r'\1${inst}_group_${i}_lane_${n}_dqsn_nff_clk\2', text)
        assert count in (0, 2), 'Unexpected DQSN declaration count'
        return text
    original_maps = maps(raw)
    output.mkdir(parents=True)
    for path in raw.rglob('*'):
        relative = pattern.sub(lambda match: rename[match[0]], str(path.relative_to(raw)))
        target = output / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_text(transform(path.read_text()))
            except UnicodeDecodeError:
                shutil.copy2(path, target)
    if descriptor:
        if target_descriptor != descriptor:
            shutil.copy2(descriptor, target_descriptor)
    assert not declarations(output, 'module') & b0mods, 'Module namespace collision'
    assert not ((declarations(output, 'proc') & b0procs) - {'min', 'max'}), 'Tcl procedure collision'
    assert maps(output) == original_maps, 'Namespacing changed PH2 map'
    metadata = list(output.rglob('*timing_ip_parameters.tcl'))
    assert len(metadata) == 1, 'Expected one timing parameter file'
    text = metadata[0].read_text()
    for field, values in original_maps.items():
        actual = dict(re.findall(r'^set var\(MAP_INDEX_TO_' + field + r'_(\d+)\)\s+(-?\d+)', text, re.M))
        assert actual == values, 'Timing metadata and PH2 mapping disagree: ' + field
    count = sum(len(re.findall(r'-name "\$\{inst\}_group_\$\{i\}_lane_\$\{n\}_dqsn_nff_clk"',
                               p.read_text())) for p in output.rglob('*.sdc'))
    assert count == 2, 'Expected two per-instance DQSN declarations'
    qip = output / 'dual3_phy_probe_phy.qip'
    assert qip.is_file(), 'Missing component QIP'
    for reference in re.findall(r'\[file join \$::quartus\(qip_path\) "([^\"]+)"\]', qip.read_text()):
        assert (output / reference).exists(), 'Unresolved QIP reference: ' + reference
    libraries = lambda p: set(re.findall(r'-library "([^\"]+)"', p.read_text()))
    assert not libraries(qip) & libraries(bank0 / 'phy_probe_phy.qip'), 'QIP library collision'
    record = {'all_pass': True, 'scope': 'Namespace and map/clock/QIP structure only; no generation, fit, simulation or hardware qualification',
              'descriptor_sha256': sha(descriptor) if descriptor else None,
              'PH2_map_entries_per_field': 96, 'dqsn_scoped_declarations': count,
              'source_sha256': {str(p.relative_to(raw)): sha(p) for p in sorted(raw.rglob('*')) if p.is_file()},
              'output_sha256': {str(p.relative_to(output)): sha(p) for p in sorted(output.rglob('*')) if p.is_file()}}
    (output / 'namespace_verification.json').write_text(json.dumps(record, indent=2) + '\n')
    print('PASS bank3 namespace, 96x3 mapping, metadata, DQSN clocks, QIP closure/libraries')


if __name__ == '__main__':
    main()
