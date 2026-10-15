#!/usr/bin/env python3
"""Use the preserved strict DDR validator with the native1600 DMA clock."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).with_name('check_hardware_log.py')
    spec = importlib.util.spec_from_file_location('preserved_dual_ddr_validator', source)
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    # The regenerated controller's DMA cycle counter uses sys_clk, driven
    # by the measured 200 MHz PHY native clock. Keep the original source.
    base.DDR_CLOCK_HZ = 200_000_000
    raw = args.log.read_bytes()
    report = base.validate(raw.decode('utf-8'), hashlib.sha256(raw).hexdigest())
    report['profile'] = 'native1600_CK800_native200'
    report['assumptions'] = [
        'Bandwidth estimates use the nominal 200 MHz DMA/sys_clk; require '
        'the matching native1600 image and separate measured clock evidence.'
    ]
    report['inputs_sha256']['native1600_wrapper'] = hashlib.sha256(
        Path(__file__).read_bytes()).hexdigest()
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(f'PASS native1600 strict diagnostic: {report["counts"]["log_lines"]} lines')


if __name__ == '__main__':
    main()
