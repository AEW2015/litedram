"""Fetch pinned source dependencies into ignored directories; no FPGA access."""
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def main():
    pins = json.loads((HERE/'config/dependencies.json').read_text())
    target = HERE/'third_party'
    target.mkdir(exist_ok=True)
    for name in ('migen', 'litex'):
        path = target/name
        pin = pins[name]
        if not path.exists():
            path.mkdir()
            run('git', 'init', '--quiet', str(path))
            run('git', 'remote', 'add', 'origin', pin['source'], cwd=path)
            run('git', 'fetch', '--quiet', '--depth=1', 'origin', pin['commit'], cwd=path)
            run('git', 'checkout', '--quiet', '--detach', 'FETCH_HEAD', cwd=path)
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=path, text=True).strip()
        if actual != pin['commit']:
            raise RuntimeError(f'{name}: existing checkout differs from pinned commit')
    patch = HERE/'patches/migen-simulator.patch'
    migen = target/'migen'
    applied = subprocess.run(['git','apply','--reverse','--check',str(patch)],
        cwd=migen, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not applied:
        run('git','apply','--check',str(patch),cwd=migen)
        run('git','apply',str(patch),cwd=migen)
    litedram = target/'litedram'
    repository = HERE.parents[1]
    if not (repository/'litedram/core/bankmachine.py').is_file():
        raise RuntimeError('Keep this overlay in the LiteDRAM fork under contrib/agilex5')
    if not litedram.exists():
        litedram.symlink_to('../../..', target_is_directory=True)
    if litedram.resolve() != repository:
        raise RuntimeError('Existing LiteDRAM dependency points to another checkout')
    print('Pinned dependencies prepared. All outputs are in ignored third_party/.')


if __name__ == '__main__':
    main()
