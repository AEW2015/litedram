#!/usr/bin/env python3
"""Strict native2133 validation with bank-independent order and profile marker checks."""
import argparse
import hashlib
import json
from pathlib import Path

import check_hardware_log_native2133_v1 as v1


def policy(text):
    result = v1.policy(text)
    markers = {0: 0, 1: 0}
    for number, line in enumerate(text.splitlines(), 1):
        match = v1.base.BANK_RE.match(line)
        if not match:
            raise v1.base.ValidationError(f'line {number}: missing bank prefix')
        bank = int(match.group(1))
        body = line[match.end():]
        occurrences = body.split().count('CONTROLLER_COMPATIBILITY')
        if occurrences:
            if occurrences != 1 or not body.startswith('CONTROLLER_COMPATIBILITY '):
                raise v1.base.ValidationError(
                    f'line {number}: expected one compatibility record per line')
            fields = v1.base.fields(body)
            expected = {
                'base_read_latency': '25',
                'frontend_read_latency': '27',
            }
            if any(fields.get(key) != value for key, value in expected.items()):
                raise v1.base.ValidationError(
                    f'line {number}: compatibility marker does not match base25/front27 profile')
            markers[bank] += 1
        alignment_occurrences = body.split().count('NATIVE2133_ALIGNMENT')
        if alignment_occurrences:
            if alignment_occurrences != 1 or not body.startswith('NATIVE2133_ALIGNMENT '):
                raise v1.base.ValidationError(
                    f'line {number}: expected one alignment record per line')
            if v1.base.fields(body).get('model_profile') != 'base25_frontend27_trial':
                raise v1.base.ValidationError(
                    f'line {number}: alignment marker does not match base25/front27 profile')
    if any(markers[bank] != 1 for bank in (0, 1)):
        raise v1.base.ValidationError('expected one base25/front27 compatibility marker per bank')
    return {'native2133': result, 'compatibility_markers_per_bank':
            {str(bank): markers[bank] for bank in (0, 1)}}


def validate(raw):
    text = raw.decode('utf-8')
    profile = policy(text)
    v1.base.DDR_CLOCK_HZ = 800_000_000 / 3
    report = v1.base.validate(text, hashlib.sha256(raw).hexdigest())
    report['profile'] = 'native2133_CK3200over3_native800over3_alignment1_base25_front27'
    report['conditional_profile_policy'] = profile
    report['inputs_sha256']['native2133_wrapper_v3'] = hashlib.sha256(
        Path(__file__).read_bytes()).hexdigest()
    report['assumptions'] = [
        'Bandwidth uses nominal native/sys_clk 800000000/3 Hz; require separate measured-clock evidence.',
        'Successful source-guarded serialized reads and alignment1 are required; queued DMA is checked by data, counts and flags.',
        'The per-bank compatibility marker must identify base/front latency 25/27.',
        'Nominal functional evidence does not qualify external I/O, CDC or PVT.'
    ]
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.log.read_bytes())
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(f'PASS native2133 strict diagnostic: {report["counts"]["log_lines"]} lines')
