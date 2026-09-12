"""Process-lifetime write exclusion, with a fail-closed old-writer fence.

The kernel lock is never deleted.  The old existence-lock name is retained as
a protocol fence, so an older installed writer cannot race a migrated store.
Only a well-formed legacy lock whose process has exited is migrated.
"""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import stat

SCHEMA = 'personal-kb.operation-guard.v2'
KERNEL = '.operation.guard'
LEGACY = '.operation.lock'
FENCE = {'schema': SCHEMA, 'lock_file': KERNEL, 'legacy_writers': 'blocked'}


class GuardError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise GuardError('unsafe_lock', '操作状态文件不是普通文件；保留现场，请检查路径。')
    return True


def _process_state(pid):
    if type(pid) is not int or not 0 < pid <= 0x7fffffff:
        return 'unknown'
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return 'dead' if ctypes.get_last_error() == 87 else 'unknown'
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return 'unknown'
            return 'alive' if code.value == 259 else 'dead'
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # Query existence; never terminate another process.
    except ProcessLookupError:
        return 'dead'
    except (PermissionError, OverflowError, OSError):
        return 'unknown'
    return 'alive'


def _legacy_state(root):
    path = root / LEGACY
    if not _regular(path):
        return {'status': 'missing'}
    try:
        if path.stat().st_size > 4096:
            raise ValueError('oversized')
        value = json.loads(path.read_text(encoding='utf-8'))
        if value == FENCE:
            return {'status': 'migrated'}
        if not isinstance(value, dict) or value.get('schema'):
            raise ValueError('unknown protocol')
        state = _process_state(value.get('pid'))
        return {'status': 'legacy_' + state, 'pid': value.get('pid')}
    except (ValueError, UnicodeError):
        return {'status': 'legacy_unknown'}


def _kernel_lock(handle, release=False):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _open(path, create):
    _regular(path)
    flags = os.O_RDWR | (os.O_CREAT if create else 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    os.set_inheritable(fd, False)
    return os.fdopen(fd, 'r+b')


def _fence(root):
    path = root / LEGACY
    state = _legacy_state(root)
    if state['status'] == 'migrated':
        return
    if state['status'] == 'missing':
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            # A legacy writer won the race. Never replace its new lock.
            return _fence(root)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(FENCE, f, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        return
    if state['status'] != 'legacy_dead':
        raise GuardError(state['status'], '知识库正在执行另一项操作，或旧版操作状态无法安全确认；请让旧操作退出后重试。不要删除锁文件。')
    # The old owner is definitely gone; leave an auditable copy and atomically
    # migrate the record. This is a protocol change, not a deletion fallback.
    import hashlib
    raw = path.read_bytes()
    backup = root / ('.operation.legacy-' + hashlib.sha256(raw).hexdigest() + '.json')
    if not backup.exists():
        with backup.open('xb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
    temp = root / '.operation.fence-next'
    _regular(temp)
    with temp.open('w', encoding='utf-8') as f:
        json.dump(FENCE, f, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)


@contextmanager
def hold(root: Path, operation: str):
    root = Path(root)
    if root.is_symlink():
        raise GuardError('unsafe_root', '知识库操作路径不能是符号链接。')
    root.mkdir(parents=True, exist_ok=True)
    with _open(root / KERNEL, True) as handle:
        try:
            _kernel_lock(handle)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise GuardError('busy', '知识库正在执行另一项建立、更新或修复操作；请稍后重试。') from exc
            raise GuardError('lock_unavailable', '无法取得知识库操作锁；请检查文件权限。') from exc
        try:
            _fence(root)
            yield
        finally:
            _kernel_lock(handle, release=True)


def inspect(root: Path) -> dict:
    """Read-only: neither create files nor migrate legacy ownership records."""
    root = Path(root)
    try:
        if _regular(root / KERNEL):
            with _open(root / KERNEL, False) as handle:
                try:
                    _kernel_lock(handle)
                except OSError as exc:
                    return {'status': 'busy', 'ready': False, 'message': '当前有写操作，稍后重试。'}
                try:
                    state = _legacy_state(root)
                finally:
                    _kernel_lock(handle, release=True)
        else:
            state = _legacy_state(root)
    except (OSError, GuardError) as exc:
        return {'status': 'unavailable', 'ready': False, 'message': str(exc)}
    status = state['status']
    return {**state, 'ready': status in ('missing', 'migrated'),
            'safe_migration_available': status == 'legacy_dead',
            'message': {'legacy_dead': '旧操作已退出，残留状态可通过更新或检查修复安全迁移。',
                        'legacy_alive': '旧版操作仍在运行，请等待它退出。',
                        'legacy_unknown': '无法确认旧操作是否仍在运行；保留状态，请宿主核对旧进程。'}.get(status, '写入锁可用。')}
