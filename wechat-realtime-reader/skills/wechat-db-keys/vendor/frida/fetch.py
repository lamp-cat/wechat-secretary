#!/usr/bin/env python3
"""Download the pinned official frida-inject binaries and verify sha256.

Stdlib only (urllib + lzma). Never uses pip/PyPI. Usage:
  python3 fetch.py [--arch host|arm64|x86_64|all] [--from DIR] [--dest DIR]   # --from: local .xz files
"""
import argparse
import hashlib
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

VERSION = '17.25.2'
HERE = Path(__file__).resolve().parent
URL = 'https://github.com/frida/frida/releases/download/{v}/{f}'


def sums():
    out = {}
    for line in (HERE / 'SHA256SUMS').read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            digest, name = line.split()[:2]
            out[name] = digest
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def host_arch():
    try:
        if subprocess.run(['sysctl', '-n', 'hw.optional.arm64'], capture_output=True,
                          text=True, timeout=5).stdout.strip() == '1':
            return 'arm64'
    except Exception:
        pass
    return 'arm64' if os.uname().machine in ('arm64', 'aarch64') else 'x86_64'


def fetch(arch, src_dir=None, dest=HERE):
    table = sums()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f'frida-inject-{arch}'
    if target.is_file() and sha256(target) == table[target.name]:
        print(f'ok: {target.name} already present and verified')
        return target
    xz_name = f'frida-inject-{VERSION}-macos-{arch}.xz'
    xz = dest / (xz_name + '.part')
    if src_dir:
        shutil.copyfile(Path(src_dir) / xz_name, xz)
    else:
        print(f'downloading {xz_name} from GitHub ...')
        with urllib.request.urlopen(URL.format(v=VERSION, f=xz_name), timeout=120) as r, open(xz, 'wb') as f:
            shutil.copyfileobj(r, f)
    if sha256(xz) != table[xz_name]:
        xz.unlink(missing_ok=True)
        raise SystemExit(f'sha256 mismatch for {xz_name}; refusing to use it')
    tmp = target.with_suffix('.tmp')
    with lzma.open(xz) as src, open(tmp, 'wb') as dst:
        shutil.copyfileobj(src, dst)
    xz.unlink(missing_ok=True)
    if sha256(tmp) != table[target.name]:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f'sha256 mismatch for decompressed {target.name}')
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    print(f'ok: {target.name} verified')
    return target


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--arch', choices=['arm64', 'x86_64', 'all', 'host'], default='host')
    p.add_argument('--from', dest='src', help='directory containing the official .xz assets')
    p.add_argument('--dest', type=Path, default=HERE, help='output directory (default: this directory)')
    a = p.parse_args()
    archs = ['arm64', 'x86_64'] if a.arch == 'all' else [host_arch() if a.arch == 'host' else a.arch]
    for arch in archs:
        fetch(arch, a.src, a.dest.expanduser())


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # network/proxy errors: short, no secrets involved
        print(f'fetch failed: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(1)
