#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Run fresh Vivado device queries in an isolated build directory."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from .topology import vivado_query, parse_vivado_map
from .auxiliary import vivado_auxiliary_query, parse_auxiliary_map


def tcl_path(path):
    value = Path(path).resolve().as_posix()
    if any(c in value for c in '{}\n\r'):
        raise ValueError('Path cannot be represented safely as a Tcl literal')
    return '{' + value + '}'


def query_device(pin_map, output_dir, *, vivado='vivado', timeout=600):
    """Return validated physical and auxiliary maps; never read a previous cache.

    The selected Vivado installation provides both the device database and
    recorded version. Every invocation gets a new directory, and failed queries
    leave their logs there for diagnosis. This does not implement a user design.
    """
    executable = shutil.which(str(vivado))
    if executable is None:
        raise OSError(f'Vivado executable not found: {vivado}')
    parent = Path(output_dir).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='usnative-query-', dir=parent))
    stub = directory / 'query_top.v'
    stub.write_text('module query_top(input a, output b); assign b = a; endmodule\n')
    version_file = directory / 'version.txt'
    prefix = (f'create_project -in_memory -part {pin_map.part}\n'
              f'read_verilog {tcl_path(stub)}\n'
              f'synth_design -top query_top -part {pin_map.part}\n'
              f'set vf [open {tcl_path(version_file)} w]\n'
              'puts $vf [version -short]\nclose $vf\n')

    def run(name, body):
        script = directory / (name + '.tcl')
        script.write_text(prefix + body + '\nclose_design\n')
        log = directory / (name + '_console.log')
        with log.open('wb') as stream:
            result = subprocess.run([executable, '-mode', 'batch', '-source', str(script),
                '-log', str(directory / (name + '.log')),
                '-journal', str(directory / (name + '.jou'))],
                cwd=directory, stdin=subprocess.DEVNULL, stdout=stream,
                stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'Vivado native query failed: {log}')

    physical = directory / 'physical.tsv'
    run('physical', vivado_query(pin_map) + '\nusnative_query ' + tcl_path(physical))
    version = version_file.read_text().strip()
    sites = parse_vivado_map(pin_map, physical.read_text(), vivado_version=version)
    auxiliary = directory / 'auxiliary.tsv'
    run('auxiliary', vivado_auxiliary_query(pin_map, sites) +
        '\nusnative_auxiliary_query ' + tcl_path(auxiliary))
    if version_file.read_text().strip() != version:
        raise RuntimeError('Vivado version changed during native query')
    aux = parse_auxiliary_map(pin_map, sites, auxiliary.read_text(), vivado_version=version)
    provenance = dict(part=pin_map.part, pin_fingerprint=pin_map.fingerprint,
        vivado_version=version, executable=executable, fresh_device_queries=True,
        files={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (physical, auxiliary)})
    (directory / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    return sites, aux, directory
