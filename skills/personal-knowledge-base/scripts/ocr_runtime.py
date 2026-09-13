#!/usr/bin/env python3
"""Stdlib-only facade. Only explicit prepare may install/download anything."""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
# This facade is also loaded via runpy/spec by the OCR worker. Resolve the
# sibling helper directly rather than depending on the caller's sys.path.
import importlib.util
_quiet_spec = importlib.util.spec_from_file_location('_pkb_quiet_process', Path(__file__).with_name('installer_process.py'))
_quiet_module = importlib.util.module_from_spec(_quiet_spec)
_quiet_spec.loader.exec_module(_quiet_module)
quiet_subprocess_kwargs = _quiet_module.quiet_subprocess_kwargs
_owned_spec = importlib.util.spec_from_file_location('_pkb_owned_process', Path(__file__).with_name('owned_process.py'))
_owned_module = importlib.util.module_from_spec(_owned_spec)
_owned_spec.loader.exec_module(_owned_module)
run_owned = _owned_module.run
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import venv
import re
import uuid

REF = Path(__file__).resolve().parents[1] / 'references'
LOCK = REF / 'ocr-requirements.lock'
MANIFEST = REF / 'ocr-models.json'
WORKER = Path(__file__).with_name('ocr_worker.py')
TIMEOUT = 180


class OCRInputLimitError(RuntimeError):
    """Input preparation is required; rerunning prepare will not fix it."""
    code = 'image_input_limit_exceeded'
    repair_required = False



def runtime_home() -> Path:
    override = os.environ.get('PERSONAL_KB_OCR_HOME')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'win32':
        root = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    elif sys.platform == 'darwin':
        root = Path.home() / 'Library' / 'Application Support'
    else:
        root = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
    return root / 'personal-kb' / 'ocr-runtime-v1'


def _runtime(home: Path, marker=None) -> Path:
    if marker is None:
        path = home / 'ready.json'
        if not path.exists():
            return home  # legacy runtime or private, unpublished build
        marker = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(marker, dict) or marker.get('state', 'ready') != 'ready':
        raise RuntimeError('OCR runtime is building/not ready; prepare required')
    generation = marker.get('generation')
    if generation is None:
        return home  # compatibility with the original ready marker
    if not isinstance(generation, str) or not re.fullmatch('[0-9a-f]{32}', generation):
        raise RuntimeError('Invalid OCR runtime generation')
    result = home / 'generations' / generation
    if result.is_symlink() or result.parent.is_symlink() or not result.is_dir():
        raise RuntimeError('Invalid OCR runtime generation directory')
    return result


def _write_marker(home: Path, marker: dict) -> None:
    temp = home / 'ready.json.tmp'
    with temp.open('w', encoding='utf-8') as f:
        json.dump(marker, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, home / 'ready.json')
    if os.name != 'nt':
        fd = os.open(home, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _python(home: Path) -> Path:
    home = _runtime(home)
    return home / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def _setup(home: Path) -> str:
    interpreter = [sys.executable] if (3, 12) <= sys.version_info[:2] < (3, 14) else (['py', '-3.12'] if os.name == 'nt' else [shutil.which('python3.12') or 'python3.12'])
    args = [*interpreter, str(Path(__file__).resolve()), 'prepare', '--home', str(home)]
    return subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)


def _digest(path: Path) -> str:
    with path.open('rb') as f:
        digest = hashlib.sha256()
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()


def _spec() -> dict:
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def _fingerprint() -> str:
    return hashlib.sha256(LOCK.read_bytes() + MANIFEST.read_bytes()).hexdigest()


def _stat(path: Path) -> list:
    s = path.stat()
    return [s.st_size, s.st_mtime_ns]


def _expected_versions() -> dict:
    result = {}
    for line in LOCK.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        if ';' in line and sys.platform != 'win32':
            continue
        name, version = line.split(';')[0].strip().split('==')
        result[name.lower().replace('_', '-')] = version
    return result


def _check_versions(home: Path) -> None:
    paths = [home / 'venv' / 'Lib' / 'site-packages'] if os.name == 'nt' else list((home / 'venv' / 'lib').glob('python*/site-packages'))
    actual = {d.metadata['Name'].lower().replace('_', '-'): d.version
              for d in importlib.metadata.distributions(path=[str(p) for p in paths])}
    for name, version in _expected_versions().items():
        if actual.get(name) != version:
            raise RuntimeError(f'OCR dependency missing/mismatched: {name}=={version}')


def probe() -> dict:
    """Read metadata/stat only: no process, model load, writes or network.

    Full model hashes are checked before every recognition, not on this cheap probe.
    """
    home = runtime_home()
    result = dict(ready=False, reason='', setup_command=_setup(home), home=str(home),
                  engine='rapidocr', engine_version='3.9.2', provider='CPUExecutionProvider',
                  model_id=_spec()['model_id'], integrity='stat-only; SHA256 before recognition')
    try:
        marker = json.loads((home / 'ready.json').read_text(encoding='utf-8'))
        home = _runtime(home, marker)
        if not _python(home).is_file():
            raise RuntimeError('OCR runtime is not prepared')
        if marker.get('fingerprint') != _fingerprint():
            raise RuntimeError('OCR runtime lock changed; prepare required')
        _check_versions(home)
        for item in _spec()['models'].values():
            if _stat(home / 'models' / item['file']) != marker['models'][item['file']]:
                raise RuntimeError(f"OCR model changed/incomplete: {item['file']}")
        result.update(ready=True, reason='Prepared (metadata checked; models not loaded)')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        result['reason'] = str(exc)
    return result


def verify_models(home: Path) -> None:
    for item in _spec()['models'].values():
        path = home / 'models' / item['file']
        if not path.is_file() or _digest(path) != item['sha256']:
            raise RuntimeError(f"OCR model missing or SHA256 mismatch: {item['file']}")


def _run_worker(home: Path, args: list[str], *, timeout: float | None = None) -> dict:
    env = os.environ.copy()
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2', MKL_NUM_THREADS='2')
    home = _runtime(home)
    limit = TIMEOUT if timeout is None else timeout
    try:
        proc = run_owned([str(_python(home)), '-B', '-X', 'utf8', str(WORKER), '--home', str(home), *args],
                         timeout=limit, env=env)
    except subprocess.TimeoutExpired as exc:
        details = getattr(exc, 'details', {'code': 'worker_timeout', 'cleanup_confirmed': False})
        if details.get('cleanup_confirmed') is not True:
            error = _owned_module.ProcessControlError('process_cleanup_unconfirmed', **{k:v for k,v in details.items() if k != 'code'})
            raise error from exc
        raise RuntimeError('OCR worker timeout; owned processes terminated and checked: ' + json.dumps(details)) from exc
    if proc.returncode:
        try:
            problem = json.loads(proc.stdout)
        except (ValueError, TypeError):
            problem = {}
        if (isinstance(problem, dict) and problem.get('code') == 'image_input_limit_exceeded'
                and problem.get('repair_required') is False):
            raise OCRInputLimitError('图片超过当前直接OCR输入上限；这不是模型损坏，不需prepare。'
                '长截图请保留原件并使用经过核验的分区或派生文档流程；不能只缩图、丢弃失败区域或降低质量门槛。'
                '当前尚无自动长图分区和图片视觉复核入口。')
        raise RuntimeError(f'OCR worker failed: {(proc.stderr or proc.stdout)[-3000:]}')
    try:
        value = json.loads(proc.stdout)
        if not isinstance(value, dict):
            raise ValueError('Expected object response')
        value['process_control'] = getattr(proc, 'process_control', {'scope': 'test_double'})
        return value
    except ValueError as exc:
        raise RuntimeError(f'OCR worker returned invalid JSON: {proc.stdout[-500:]}') from exc


def read_native_pdf(path: Path, page_index: int = 0, *, mode: str = 'text') -> dict:
    """Reuse verified local runtime capabilities; no prepare/download/model inference."""
    if mode not in {'text', 'geometry', 'info'} or type(page_index) is not int or page_index < 0:
        raise ValueError('Invalid native PDF request')
    state = probe()
    if not state['ready']:
        raise RuntimeError('native_reader_runtime_unavailable: ' + state['reason'])
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise RuntimeError('Native PDF input must be a local file')
    return _run_worker(runtime_home(), ['native-' + mode, str(path), '--page-index', str(page_index)], timeout=45)


def _recognize(path: Path, page_index: int | None) -> dict:
    home = runtime_home()
    try:
        state = probe()
        if not state['ready']:
            raise RuntimeError(state['reason'])
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise RuntimeError('OCR input must be a local file')
        if page_index is not None and (type(page_index) is not int or page_index < 0):
            raise RuntimeError('page_index must be a zero-based nonnegative integer')
        # Worker verifies hashes itself, avoiding a parent/worker integrity gap.
        args = ['image', str(path)] if page_index is None else ['pdf-page', str(path), '--page-index', str(page_index)]
        return _run_worker(home, args)
    except (OSError, RuntimeError) as exc:
        if getattr(exc, 'abort_operation', False) or isinstance(exc, OCRInputLimitError):
            raise
        raise RuntimeError(f'{exc}\nSetup/repair: {_setup(home)}') from exc


def recognize_pdf_page(path: Path, page_index: int) -> dict:
    return _recognize(path, page_index)


def recognize_image(path: Path) -> dict:
    return _recognize(path, None)


@contextlib.contextmanager
def _prepare_lock(home: Path):
    """Nonblocking OS lock, automatically released on close or process death.

    Keep the file: unlinking it could let concurrent callers lock different inodes.
    The descriptor is non-inheritable and is not passed to pip/OCR subprocesses.
    Each build has its own directory, so orphan children never share a new build.
    """
    path = home / 'prepare.lock'
    with path.open('a+b') as lock:
        os.set_inheritable(lock.fileno(), False)
        try:
            if os.name == 'nt':
                import msvcrt
                lock.seek(0)
                # Windows permits a byte-range lock beyond EOF, even on an empty
                # file. Do not write/truncate a byte that another caller may lock.
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError(f'Another prepare is active for {home}; retry after it exits') from exc
            raise RuntimeError(f'Cannot acquire OCR prepare file lock: {exc}') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _compatible_ready(home: Path) -> bool:
    """Check whether a published generation may remain active during a rebuild.

    Unlike the cheap probe, preparation verifies model hashes as well as the
    current lock, versions and recorded file stats. Unknown/corrupt/incompatible
    generations do not qualify. This is not a new OCR accuracy certification.
    The caller holds prepare.lock; no network, subprocess or writes occur here.
    """
    try:
        marker = json.loads((home / 'ready.json').read_text(encoding='utf-8'))
        if not isinstance(marker, dict) or marker.get('fingerprint') != _fingerprint():
            return False
        active = _runtime(home, marker)
        if not _python(active).is_file():
            return False
        _check_versions(active)
        verify_models(active)
        for item in _spec()['models'].values():
            if _stat(active / 'models' / item['file']) != marker['models'][item['file']]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def prepare(home: Path) -> dict:
    """Explicit network boundary; repair corrupt files atomically, preserve other files."""
    if not (3, 12) <= sys.version_info[:2] < (3, 14):
        raise RuntimeError('Use Python 3.12 or 3.13 to prepare this locked runtime (3.12 recommended)')
    home.mkdir(parents=True, exist_ok=True)
    with _prepare_lock(home):
        published_home = home
        preserve_ready = _compatible_ready(home)
        generation = uuid.uuid4().hex
        # Keep a compatible, hash-verified active generation until the candidate
        # passes all checks. Failed pip/download/self-check must not disable it.
        # A missing, corrupt or incompatible runtime remains fail-closed.
        if not preserve_ready:
            _write_marker(home, {'state': 'building', 'generation': generation})
        # Each build still owns a separate unpublished directory. Orphan children
        # cannot write either the active generation or a subsequent build.
        home = home / 'generations' / generation
        home.mkdir(parents=True, exist_ok=False)
        warnings = []
        env = os.environ.copy()
        env.update(PYTHONNOUSERSITE='1', PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_CACHE_DIR='1')
        if not _python(home).is_file():
            # EnvBuilder's with_pip path strips PYTHONPATH internally. Bootstrap
            # explicitly so the host's sitecustomize protection remains active.
            venv.EnvBuilder(with_pip=False).create(home / 'venv')
            subprocess.run([str(_python(home)), '-m', 'ensurepip', '--upgrade', '--default-pip'],
                           check=True, stdout=sys.stderr, env=env, timeout=900, **quiet_subprocess_kwargs())
        subprocess.run([str(_python(home)), '-m', 'pip', 'install', '--no-input', '-r', str(LOCK)],
                       check=True, stdout=sys.stderr, env=env, timeout=900, **quiet_subprocess_kwargs())
        subprocess.run([str(_python(home)), '-m', 'pip', 'check'], check=True, stdout=sys.stderr, env=env, timeout=900, **quiet_subprocess_kwargs())
        _check_versions(home)
        models = home / 'models'
        if models.is_symlink() or (hasattr(models, 'is_junction') and models.is_junction()):
            raise RuntimeError('Model directory must not be a link or junction')
        if not models.is_dir():
            models.mkdir(exist_ok=True)
        if models.is_symlink() or (hasattr(models, 'is_junction') and models.is_junction()):
            raise RuntimeError('Model directory must not be a link or junction')
        for item in _spec()['models'].values():
            target = home / 'models' / item['file']
            if target.is_file() and _digest(target) == item['sha256']:
                continue
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.partial', delete=False) as f:
                    tmp = Path(f.name)
                    with urllib.request.urlopen(item['url'], timeout=120) as source:
                        while chunk := source.read(1024 * 1024):
                            f.write(chunk)
                if _digest(tmp) != item['sha256']:
                    raise RuntimeError(f"Downloaded model SHA256 mismatch: {item['file']}")
                os.replace(tmp, target)
            finally:
                if tmp is not None and tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError as exc:
                        warning = {'code': 'temporary_cleanup_denied', 'path': str(tmp),
                                   'error': type(exc).__name__}
                        warnings.append(warning)
                        print(json.dumps({'warning': warning}), file=sys.stderr)
        verify_models(home)
        # Offline synthetic text must pass detection + recognition (not just a
        # blank-image detection call) before readiness is published.
        _run_worker(home, ['self-check'])
        marker = {'state': 'ready', 'generation': generation, 'fingerprint': _fingerprint(), 'models': {
            item['file']: _stat(home / 'models' / item['file']) for item in _spec()['models'].values()}}
        _write_marker(published_home, marker)
        return {'ready': True, 'home': str(published_home), 'model_id': _spec()['model_id'],
                'warnings': warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('prepare', 'check', 'image', 'pdf-page'):
        p = sub.add_parser(command)
        p.add_argument('--home', type=Path)
        if command in ('image', 'pdf-page'):
            p.add_argument('path', type=Path)
        if command == 'pdf-page':
            p.add_argument('--page', type=int, required=True, help='1-based page number')
    args = parser.parse_args()
    if args.home:
        os.environ['PERSONAL_KB_OCR_HOME'] = str(args.home.expanduser().resolve())
    try:
        if args.command == 'prepare':
            result = prepare(runtime_home())
        elif args.command == 'check':
            result = probe()
        elif args.command == 'image':
            result = recognize_image(args.path)
        else:
            result = recognize_pdf_page(args.path, args.page - 1)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ready', True) else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'{exc}\nSetup/repair: {_setup(runtime_home())}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
