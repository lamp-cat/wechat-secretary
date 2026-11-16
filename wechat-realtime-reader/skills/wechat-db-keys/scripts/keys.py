#!/usr/bin/env python3
"""Capture PBKDF candidates privately, then pair them by page-one HMAC.

Capture backends (no pip/network needed with the bundled one):
  inject  official standalone `frida-inject` (bundled in ../vendor/frida, or on PATH)
  python  the `frida` Python module, if already installed
Stdlib only otherwise. Does not decrypt/export chats.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

HOOK = r"""
'use strict';
function hex(p, n) {
  return Array.from(new Uint8Array(p.readByteArray(n)),
    b => b.toString(16).padStart(2, '0')).join('');
}
const target = Module.getGlobalExportByName('CCKeyDerivationPBKDF');
Interceptor.attach(target, {
  onEnter(args) {
    this.record = null;
    // CommonCrypto: PBKDF2=2, HMAC-SHA512=5, salt length=16.
    if (args[0].toUInt32() !== 2 || args[5].toUInt32() !== 5 ||
        args[4].toUInt32() !== 16 || args[8].toUInt32() !== 32) return;
    const rounds = args[6].toUInt32();
    if (rounds !== 256000 && rounds !== 2) return;
    try {
      this.record = {type: 'candidate', rounds: rounds,
        salt: hex(args[3], 16), input: null};
      // The 2-round MAC derivation's input can itself be the AES key.
      if (args[2].toUInt32() === 32) this.record.input = hex(args[1], 32);
      this.output = args[7];
    } catch (_) { this.record = null; }
  },
  onLeave(retval) {
    if (this.record === null || retval.toInt32() !== 0) return;
    try {
      this.record.output = hex(this.output, 32);
      send(this.record);
    } catch (_) {}
  }
});
Interceptor.flush();
send({type: 'ready'});
"""


def private_parent(path):
    path = path.expanduser().absolute()
    # Secret artifacts may only be placed under the dedicated private config dir.
    base = (Path.home() / '.config/wechat-realtime-reader').resolve()
    parent = path.parent.resolve()
    if parent != base and base not in parent.parents:
        raise ValueError('output must be inside ~/.config/wechat-realtime-reader')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (base, parent):
        if directory.stat().st_mode & 0o077:
            raise ValueError('secret directory permissions must be 0700')
    return path


VENDOR = Path(__file__).resolve().parent.parent / 'vendor' / 'frida'

# frida-inject block-buffers plain send() output on a pipe; its 'frida:stdout' channel flushes
# every message, so the inject backend wraps send() into one flushed JSON line per record.
INJECT_HOOK = ("function emit(o) { send(['frida:stdout', JSON.stringify(o) + '\\n']); }\n"
               + HOOK.replace('send(this.record)', 'emit(this.record)')
                     .replace("send({type: 'ready'})", "emit({type: 'ready'})"))


def host_arch():
    try:
        out = subprocess.run(['sysctl', '-n', 'hw.optional.arm64'], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if out == '1':
            return 'arm64'
    except Exception:
        pass
    return 'arm64' if os.uname().machine in ('arm64', 'aarch64') else 'x86_64'


def _runs(path):
    # The first launch of a freshly copied binary can take a while (macOS malware scan of ~85 MB).
    try:
        r = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=180)
        return r.returncode == 0 and r.stdout.strip()[:1].isdigit()
    except Exception:
        return False


def _unquarantine(path):
    # Files copied out of a downloaded .dmg may carry quarantine; our own copy only.
    subprocess.run(['xattr', '-d', 'com.apple.quarantine', str(path)],
                   capture_output=True, timeout=10)


def _sha256(path):
    import hashlib as _h
    h = _h.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _pinned(vendor_dir, name):
    sums = vendor_dir / 'SHA256SUMS'
    if not sums.is_file():
        return None
    for line in sums.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith('#') and parts[1] == name:
            return parts[0]
    return None


def _stage(src, digest):
    """Copy a bundled binary to a private per-user dir and run it from there.

    Running straight out of a downloaded (quarantined) .app can be refused by Gatekeeper;
    a data-only copy made by this process carries no quarantine attribute.
    """
    dest_dir = Path.home() / '.local/share/wechatdesk/frida' / digest[:16]
    dest = dest_dir / 'frida-inject'
    if dest.is_file() and _sha256(dest) == digest:
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = dest_dir / 'frida-inject.tmp'
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    return dest


def find_inject(explicit=None):
    """Return a runnable frida-inject path or None. Bundled (sha256-pinned) first, then PATH."""
    arch = host_arch()
    candidates = []  # (path, pinned digest or None)
    for value in (explicit, os.environ.get('WD_FRIDA_INJECT')):
        if value:
            candidates.append((Path(value).expanduser(), None))
    for vendor in [VENDOR] + [Path(d) for d in os.environ.get('WD_FRIDA_VENDOR_DIRS', '').split(':') if d]:
        name = f'frida-inject-{arch}'
        candidates.append((vendor / name, _pinned(vendor, name)))
    on_path = shutil.which('frida-inject')
    if on_path:
        candidates.append((Path(on_path), None))
    for cand, digest in candidates:
        if not cand.is_file():
            continue
        if digest:
            if _sha256(cand) != digest:
                continue  # never run a bundled binary that does not match the pinned release
            try:
                cand = _stage(cand, digest)
            except OSError:
                pass
        elif not os.access(cand, os.X_OK):
            continue
        if _runs(cand):
            return cand
        _unquarantine(cand)
        if _runs(cand):
            return cand
    return None


def python_frida_available():
    try:
        import frida  # noqa: F401
        return True
    except Exception:
        return False


def frida_check(args):
    inject = find_inject(args.frida_inject)
    if inject is not None:
        version = subprocess.run([str(inject), '--version'], capture_output=True, text=True,
                                 timeout=180).stdout.strip()
        where = 'bundled' if '.local/share/wechatdesk/frida' in str(inject) else 'external'
        print(f'backend=inject source={where} version={version} path={inject}')
        return
    if python_frida_available():
        import frida
        print(f'backend=python version={frida.__version__}')
        return
    print('backend=none')
    sys.exit(3)


def _target_pids(binary):
    out = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(' ')
        if cmd.strip().startswith(str(binary)) and pid.isdigit():
            pids.append(int(pid))
    return pids


def _kill_targets(binary):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = _target_pids(binary)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(1.5)


def _classify(stderr_text):
    low = stderr_text.lower()
    if 'unable to access process' in low or 'task_for_pid' in low or 'permission' in low \
            or 'not permitted' in low:
        return 'permission'
    if 'unable to spawn' in low or 'failed to spawn' in low or 'unable to find executable' in low:
        return 'spawn'
    if 'crashed' in low:
        return 'crashed'
    return 'unknown'


def capture_inject(args, inject, binary, output):
    stopped = threading.Event()
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda *_: stopped.set())
    work = Path(tempfile.mkdtemp(prefix='wd-frida-'))
    hook = work / 'hook.js'
    hook.write_text(INJECT_HOOK)
    err_path = work / 'stderr.txt'
    count, ready, failed = 0, threading.Event(), threading.Event()
    proc = None
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as log, open(err_path, 'w') as err:
            # stdout carries candidate material: it goes only to this process, never to a terminal.
            # stdin must be a pipe: frida-inject rejects /dev/null (tcgetattr ENODEV) but accepts ENOTTY.
            proc = subprocess.Popen([str(inject), '-f', str(binary), '-s', str(hook)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, text=True, bufsize=1, start_new_session=True)

            def reader():
                nonlocal count
                for line in proc.stdout:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get('type') == 'error':
                        failed.set()
                        continue
                    if message.get('type') == 'send':
                        message = message.get('payload')
                    payload = message
                    if not isinstance(payload, dict):
                        continue
                    if payload.get('type') == 'ready':
                        ready.set()
                    elif payload.get('type') == 'candidate':
                        log.write(json.dumps(payload) + '\n')
                        log.flush()
                        count += 1
                        print(f'Captured candidates: {count}', flush=True)
                stopped.set()

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            limit = time.monotonic() + 30
            while time.monotonic() < limit and not ready.is_set() and not stopped.is_set():
                time.sleep(0.1)
            if not ready.is_set() or failed.is_set():
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                err.flush()
                kind = _classify(err_path.read_text(errors='replace'))
                hints = {
                    'permission': 'frida-inject could not attach (permission). Check the temporary copy '
                                  'was signed with get-task-allow, and approve any Developer Tools prompt.',
                    'spawn': 'frida-inject could not spawn the temporary app.',
                    'crashed': 'temporary app crashed before the hook was ready.',
                    'unknown': 'hook was not ready.',
                }
                raise RuntimeError(f'[{kind}] {hints[kind]} App was not left running.')
            print('Hook ready (bundled frida-inject); temporary WeChat resumed. Ctrl-C stops capture and app.',
                  flush=True)
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline and not stopped.wait(0.1):
                if failed.is_set():
                    break
            if failed.is_set():
                raise RuntimeError('capture hook reported an error')
    finally:
        # Kill the app copy first so it cannot keep running uninstrumented.
        _kill_targets(binary)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(work, ignore_errors=True)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    print(f'Capture ended: {count} candidates.')


def capture(args):
    app = args.app.expanduser().resolve()
    binary = app / 'Contents/MacOS/WeChat'
    if not binary.is_file() or app == Path('/Applications/WeChat.app'):
        raise ValueError('provide a temporary WeChat.app copy, not the official app')
    output = private_parent(args.output)
    backend = args.backend
    inject = None
    if backend in ('auto', 'inject'):
        inject = find_inject(args.frida_inject)
        if inject is None and backend == 'inject':
            raise FileNotFoundError('frida-inject not found')
    if inject is not None:
        print(f'Using frida-inject: {"bundled" if ".local/share/wechatdesk/frida" in str(inject) else inject}',
              flush=True)
        return capture_inject(args, inject, binary, output)
    if not python_frida_available():
        print('No Frida available: bundled frida-inject missing and Python frida not installed. '
              'Run vendor/frida/fetch.py (GitHub, no pip) or reinstall the app.', file=sys.stderr)
        sys.exit(3)
    print('Using Python frida module.', flush=True)
    return capture_python(args, binary, output)


def capture_python(args, binary, output):
    import frida
    device = frida.get_local_device()
    pid = None
    session = None
    ready, failed, stopped = threading.Event(), threading.Event(), threading.Event()
    count = 0
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        # Exclusive creation: never truncate a previous capture.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as log:
            def on_message(message, _data):
                nonlocal count
                if message.get('type') == 'error':
                    failed.set()
                    print('Hook failed; stopping (details suppressed).', file=sys.stderr)
                    return
                payload = message.get('payload', {})
                if payload.get('type') == 'ready':
                    ready.set()
                elif payload.get('type') == 'candidate':
                    log.write(json.dumps(payload) + '\n')
                    log.flush()
                    count += 1
                    print(f'Captured candidates: {count}', flush=True)

            pid = device.spawn([str(binary)], stdio='pipe')
            session = device.attach(pid)
            session.on('detached', lambda *_: stopped.set())
            script = session.create_script(HOOK)
            script.on('message', on_message)
            script.load()
            if not ready.wait(5) or failed.is_set() or stopped.is_set():
                raise RuntimeError('hook was not ready; app was not resumed')
            print('Hook ready; resuming temporary WeChat. Ctrl-C stops capture and app.', flush=True)
            device.resume(pid)
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline and not stopped.wait(0.1):
                if failed.is_set():
                    break
            if failed.is_set():
                raise RuntimeError('capture hook failed')
    finally:
        # Kill before detach so the copy cannot keep running uninstrumented.
        if pid is not None:
            try:
                device.kill(pid)
            except frida.ProcessNotFoundError:
                pass
        if session is not None:
            try:
                session.detach()
            except frida.InvalidOperationError:
                pass
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    print(f'Capture ended: {count} candidates. Check for remaining helper processes.')


def authenticates(key, page):
    if len(page) != 4096 or len(key) != 32:
        return False
    mac_salt = bytes(b ^ 0x3a for b in page[:16])
    mac_key = hashlib.pbkdf2_hmac('sha512', key, mac_salt, 2, 32)
    actual = hmac.new(mac_key, page[16:4032] + struct.pack('<I', 1), 'sha512').digest()
    return hmac.compare_digest(actual, page[4032:4096])


def pair(args):
    candidates = set()
    with args.capture.expanduser().open() as log:
        for line in log:
            record = json.loads(line)
            for field in ('input', 'output'):
                value = record.get(field)
                if isinstance(value, str) and len(value) == 64:
                    candidates.add(bytes.fromhex(value))
    root = args.db_root.expanduser().resolve(strict=True)
    result, missing = {}, 0
    for db in sorted(root.rglob('*.db')):
        if db.is_symlink():
            continue
        with db.open('rb') as source:
            page = source.read(4096)
        if page.startswith(b'SQLite format 3\0'):
            continue
        hits = [key for key in candidates if authenticates(key, page)]
        name = db.relative_to(root).as_posix()
        if len(hits) == 1:
            result[name] = hits[0].hex()
        else:
            missing += 1
    if not result:
        raise ValueError('no authenticated matches; existing keys were not changed')
    output = private_parent(args.output)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as dest:
        json.dump(result, dest, indent=2)
        dest.write('\n')
    print(f'Authenticated databases: {len(result)}; unmatched/ambiguous: {missing}.')
    print('New key file written privately; existing keys.json was not changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    cap = commands.add_parser('capture', help='run a temporary app with a startup hook')
    cap.add_argument('--app', type=Path, required=True)
    cap.add_argument('--output', type=Path, required=True)
    cap.add_argument('--seconds', type=int, default=30, choices=range(1, 121), metavar='1..120')
    cap.add_argument('--backend', choices=['auto', 'inject', 'python'], default='auto')
    cap.add_argument('--frida-inject', help='path to a frida-inject binary (default: bundled)')
    cap.set_defaults(run=capture)
    chk = commands.add_parser('frida-check', help='report which Frida backend would be used (no secrets)')
    chk.add_argument('--frida-inject')
    chk.set_defaults(run=frida_check)
    match = commands.add_parser('pair', help='authenticate candidates against database first pages')
    match.add_argument('--capture', type=Path, required=True)
    match.add_argument('--db-root', type=Path, required=True)
    match.add_argument('--output', type=Path, required=True)
    match.set_defaults(run=pair)
    args = parser.parse_args()
    os.umask(0o077)
    args.run(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never echo exception contents: a library may include secret input.
        detail = str(error) if isinstance(error, RuntimeError) and str(error).startswith('[') else ''
        print(f'Operation failed ({type(error).__name__}) {detail}; see Skill troubleshooting.', file=sys.stderr)
        sys.exit(1)
