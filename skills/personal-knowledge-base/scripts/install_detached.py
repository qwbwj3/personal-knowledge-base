"""One bounded Windows resume process, independent of the host's console/job.

No scheduler, elevation, host termination, or code-copy fallback. A denied job
breakaway is a reported limitation, never retried as an attached child.
"""
from __future__ import annotations
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

WINDOWS_FLAGS = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED, NEW_GROUP, BREAKAWAY


def outside_windows_job():
    if sys.platform != 'win32':
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                     ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    member = wintypes.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(member)):
        raise ctypes.WinError(ctypes.get_last_error())
    return not member.value


def launch(lifecycle, ident):
    import install_lifecycle as m
    if sys.platform != 'win32':
        m.refuse('detached_resume_windows_only', '此入口仅用于Windows；其他平台使用正常前台续装。')
    lifecycle.version(ident)
    result, code = lifecycle.run('status')
    if code != 3 or (result.get('pending') or {}).get('id') != ident:
        m.refuse('pending_activation_required')
    lifecycle.anchor.verify(lifecycle.version(ident) / 'code', lifecycle.receipt(ident)['commit'])
    # Never inherit target as cwd (it could itself hold the directory open).
    with m.lock(lifecycle.manager / 'resume-launch.lock'):
        # A live worker holds this for its lifetime, making repeat launches explicit.
        with m.lock(lifecycle.manager / 'resume-worker.lock'):
            job_id = uuid.uuid4().hex
            job = m.safe(lifecycle.manager / ('resume-job-' + job_id))
            job.mkdir()
            argv = lifecycle.resume_command(ident)
            argv[-1] = '300'
            argv += ['--detached-worker-id', job_id]
            m.write_json(job / 'request.json', {'candidate_id': ident,
                         'expected_commit': lifecycle.anchor.commit, 'source': str(lifecycle.anchor.source), 'wait_seconds': 300})
            with (job / 'result.json').open('xb') as out, (job / 'stderr.log').open('xb') as err:
                try:
                    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                        stderr=err, cwd=str(lifecycle.manager), close_fds=True,
                        creationflags=WINDOWS_FLAGS,
                        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
                except OSError as exc:
                    m.write_json(job / 'launch.json', {'status': 'launch_refused',
                                 'winerror': getattr(exc, 'winerror', None)})
                    m.refuse('detached_resume_launch_denied',
                        '系统不允许启动独立续装进程。候选保留，未完成升级；请由独立终端执行resume_argv，不降低进程隔离要求、不提权或关闭安全保护。')
            m.write_json(job / 'launch.json', {'status': 'spawned', 'pid': proc.pid})
        # Child acquires worker lock only after parent releases it.
        deadline = time.monotonic() + 8
        while not (job / 'started.json').exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.1)
        started = (job / 'started.json').exists()
        if started:
            info = m.read_json(job / 'started.json')
            started = info.get('outside_host_job') is True and info.get('pid') == proc.pid
        phase = 'verifying' if started else 'starting'
        failed = False
        if (job / 'progress.json').exists():
            state = m.read_json(job / 'progress.json')
            phase = state.get('phase', phase)
            failed = phase == 'failed' and state.get('worker_finished') is True
        return {'status': 'resume_worker_failed' if failed else 'resume_worker_started' if started else 'resume_worker_start_unconfirmed',
                'target': str(lifecycle.target), 'candidate_id': ident,
                'requested_commit': lifecycle.anchor.commit,
                'completion': {'complete': False, 'activation_complete': False},
                'worker': {'pid': proc.pid, 'independence_verified': started,
                           'result_path': str(job / 'result.json'),
                           'stderr_path': str(job / 'stderr.log'), 'progress_path': str(job / 'progress.json'), 'phase': phase, 'wait_seconds': 300},
                'resume_argv': lifecycle.resume_command(ident),
                'next_step': ('独立续装已失败，先查看result_path/stderr_path的具体步骤，不要重复启动或误报生效。' if failed else '独立续装已启动。请保存工作并正常退出WorkBuddy，最多等待300秒（另计校验耗时）。随后重开宿主，读取结果并核对status/check的实际提交；启动进程不代表升级成功。'
                              if started else '尚未确认独立续装已就绪；先查看本次结果/错误日志，不宣称已启动或生效，不重复启动。必要时使用独立终端前台续装。')}, 2 if failed else 3


@contextlib.contextmanager
def early_worker(target, source, expected_commit, job_id, candidate_id):
    """Only a liveness/independence handshake. It does NOT authorize installation.

    Runs before Anchor's expensive Git verification, from the user-selected
    trusted entrypoint. Candidate/receipt/code integrity is still checked later.
    """
    import re
    import install_lifecycle as m
    if not all(isinstance(x, str) and re.fullmatch('[0-9a-f]{32}', x)
               for x in (job_id, candidate_id)):
        m.refuse('invalid_version_id')
    target = m.safe(target)
    manager = m.safe(target.parent / ('.' + target.name + '.install-manager'))
    job = m.safe(manager / ('resume-job-' + job_id))
    request = m.read_json(job / 'request.json')
    if (request.get('candidate_id') != candidate_id or
            request.get('expected_commit') != expected_commit or
            m.safe(request.get('source', '')) != m.safe(source) or
            request.get('wait_seconds') != 300):
        m.refuse('detached_resume_request_mismatch')
    if not outside_windows_job():
        m.refuse('detached_resume_still_attached', '续装未独立于宿主任务，未执行校验或切换；请使用独立终端。')
    deadline = time.monotonic() + 10
    while True:
        guard = m.lock(manager / 'resume-worker.lock')
        try:
            guard.__enter__()
            break
        except m.Refused as exc:
            if exc.reason != 'target_busy' or time.monotonic() >= deadline:
                raise
            time.sleep(.1)
    def progress(phase, **details):
        m.write_json(job / 'progress.json', {'pid': os.getpid(), 'phase': phase,
                     'activation_complete': phase == 'activated', **details})
    try:
        progress('starting')
        m.write_json(job / 'started.json', {'pid': os.getpid(), 'outside_host_job': True,
                     'installation_authorized': False, 'phase': 'starting'})
        yield progress
    finally:
        guard.__exit__(None, None, None)


def directory_release_probe(blocker):
    """Non-mutating DELETE-access probe of an already-tagged directory move.

    False = known access/share denial, True = probe passed (not a rename promise),
    None = not Windows/unknown. Full verified transaction remains mandatory.
    """
    if sys.platform != 'win32' or not isinstance(blocker, dict) or blocker.get('operation') != 'directory_move':
        return None
    paths = blocker.get('paths') or {}
    source = paths.get('source') if isinstance(paths, dict) else None
    if not source:
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(source), 0x00010000, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        return False if ctypes.get_last_error() in (5, 32, 33) else None
    kernel.CloseHandle(handle)
    return True
