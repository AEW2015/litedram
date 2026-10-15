"""Regenerate compact source controllers and verify frozen RTL hashes; no hardware."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=HERE/'generated/controller-replay')
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    paths = [HERE/'third_party'/name for name in ('migen','litex','litedram')]
    env['PYTHONPATH'] = os.pathsep.join(map(str, paths))
    expected = json.loads((HERE/'source/expected_controllers.json').read_text())
    results = {}
    for rate, record in expected.items():
        output = args.output/rate
        subprocess.run([sys.executable, str(HERE/record['generator']), '--output', str(output)],
            cwd=HERE, env=env, check=True)
        actual = hashlib.sha256((output/'litedram_external_controller.v').read_bytes()).hexdigest()
        if actual != record['sha256']:
            raise RuntimeError(f'{rate}: controller differs from preserved source result: {actual}')
        results[rate] = dict(sha256=actual, status='PASS_BYTE_IDENTICAL', scope=record['scope'])
    (args.output/'verification.json').write_text(json.dumps(results, indent=2)+'\n')
    print('All three controller source replays match. No synthesis or hardware was run.')


if __name__ == '__main__':
    main()
