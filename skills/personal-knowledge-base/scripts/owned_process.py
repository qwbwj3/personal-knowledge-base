"""Bounded short-lived workers owned by this invocation, never detached installers.

Windows: create suspended, assign a non-inheritable kill-on-close Job, then
resume. A venv redirector and its real interpreter therefore share the Job.
No task-name kills, elevation, breakaway flags, or modification of host policy.
Output is spooled to private temporary files, not indefinitely drained pipes.
"""
from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import tempfile
import time


class ProcessControlError(RuntimeError):
    def __init__(self, code: str, **details):
        self.abort_operation = True
        self.details = {'code': code, **details}
        super().__init__(f"{code}: {details}")


class WorkerTimeout(subprocess.TimeoutExpired):
    def __init__(self, cmd, timeout, *, cleanup_confirmed, scope):
        super().__init__(cmd, timeout)
        self.details = {'code': 'worker_timeout', 'timeout_seconds': timeout,
                        'cleanup_confirmed': cleanup_confirmed, 'scope': scope,
                        'cleanup_scope': 'owned_process_execution',
                        'file_resources_released': None}


def _decode(file, limit):
    file.seek(0)
    return file.read(limit).decode('utf-8', errors='replace')


def _assign_windows_job(kernel, job, process):
    """Small API boundary; failure must keep the child suspended and fail closed."""
    return kernel.AssignProcessToJobObject(job, process)


def _windows(args, timeout, env, cwd, output, error, null, limit):
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt
    import _winapi

    class BasicLimits(C.Structure):
        _fields_ = [('PerProcessUserTimeLimit', C.c_int64), ('PerJobUserTimeLimit', C.c_int64),
                    ('LimitFlags', W.DWORD), ('MinimumWorkingSetSize', C.c_size_t),
                    ('MaximumWorkingSetSize', C.c_size_t), ('ActiveProcessLimit', W.DWORD),
                    ('Affinity', C.c_size_t), ('PriorityClass', W.DWORD), ('SchedulingClass', W.DWORD)]

    class IoCounters(C.Structure):
        _fields_ = [(name, C.c_uint64) for name in
                    ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                     'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class ExtendedLimits(C.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                    ('ProcessMemoryLimit', C.c_size_t), ('JobMemoryLimit', C.c_size_t),
                    ('PeakProcessMemoryUsed', C.c_size_t), ('PeakJobMemoryUsed', C.c_size_t)]

    class Accounting(C.Structure):
        _fields_ = [(name, C.c_int64) for name in
                    ('TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                    (name, W.DWORD) for name in
                    ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]

    kernel = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([C.c_void_p, W.LPCWSTR], W.HANDLE),
        'SetInformationJobObject': ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        'AssignProcessToJobObject': ([W.HANDLE, W.HANDLE], W.BOOL),
        'QueryInformationJobObject': ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        'TerminateJobObject': ([W.HANDLE, W.UINT], W.BOOL),
        'ResumeThread': ([W.HANDLE], W.DWORD),
        'CloseHandle': ([W.HANDLE], W.BOOL),
        'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        'IsProcessInJob': ([W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
        'CreateIoCompletionPort': ([W.HANDLE, W.HANDLE, C.c_size_t, W.DWORD], W.HANDLE),
        'GetQueuedCompletionStatus': ([W.HANDLE, C.POINTER(W.DWORD), C.POINTER(C.c_size_t), C.POINTER(C.c_void_p), W.DWORD], W.BOOL),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = argtypes, restype

    job = kernel.CreateJobObjectW(None, None)  # NULL security attributes: not inheritable.
    if not job:
        raise ProcessControlError('process_containment_unavailable', stage='create_job', winerror=C.get_last_error())
    process = thread = port = None
    assigned = False
    handles = []
    started = time.monotonic()
    members = {}  # Retained handles prevent PID reuse while waiting for exit.
    vanished = set()
    cleanup_observation = {}

    def accounting():
        info = Accounting()
        if not kernel.QueryInformationJobObject(job, 1, C.byref(info), C.sizeof(info), None):
            raise ProcessControlError('process_cleanup_unconfirmed', stage='query_job', winerror=C.get_last_error())
        return info

    def remember_members():
        # Do not infer process completion from Job accounting. A process may
        # still be finishing kernel/I/O teardown after leaving the active list.
        # Retain handles while it is alive, including the real venv interpreter.
        capacity = 64
        while True:
            class ProcessIds(C.Structure):
                _fields_ = [('NumberOfAssignedProcesses', W.DWORD),
                            ('NumberOfProcessIdsInList', W.DWORD),
                            ('ProcessIdList', C.c_size_t * capacity)]
            info = ProcessIds()
            ok = kernel.QueryInformationJobObject(job, 3, C.byref(info), C.sizeof(info), None)
            if ok and info.NumberOfProcessIdsInList >= info.NumberOfAssignedProcesses:
                break
            error_code = C.get_last_error()
            if not ok and error_code != 234:  # ERROR_MORE_DATA
                raise ProcessControlError('process_cleanup_unconfirmed', stage='job_member_list', winerror=error_code)
            capacity = max(capacity * 2, info.NumberOfAssignedProcesses)
            if capacity > 4096:
                raise ProcessControlError('process_tracking_limit_exceeded', limit=4096)
        for member_pid in info.ProcessIdList[:info.NumberOfProcessIdsInList]:
            retain_member(member_pid)
        drain_notifications()

    def retain_member(member_pid):
        if member_pid in members or member_pid in vanished:
            return
        if len(members) + len(vanished) >= 4096:
            raise ProcessControlError('process_tracking_limit_exceeded', limit=4096)
        # Synchronize/query only. Never terminate a PID from an event/snapshot.
        # A retained handle pins its identity. A departed/recycled event PID
        # counts as already gone, not as permission to kill the replacement.
        handle = kernel.OpenProcess(0x00101000, False, member_pid)
        if not handle:
            error_code = C.get_last_error()
            if error_code == 87:
                vanished.add(member_pid)
                return
            raise ProcessControlError('process_cleanup_unconfirmed', stage='retain_member', winerror=error_code)
        belongs = W.BOOL()
        if not kernel.IsProcessInJob(handle, job, C.byref(belongs)):
            kernel.CloseHandle(handle)
            raise ProcessControlError('process_cleanup_unconfirmed', stage='verify_member_identity')
        if not belongs.value:
            # A process cannot leave this Job alive. The observed original is
            # gone and this PID was recycled; do not operate on the replacement.
            kernel.CloseHandle(handle)
            vanished.add(member_pid)
            return
        members[member_pid] = handle

    def drain_notifications():
        # Attach before assignment. Polling alone misses very short-lived test
        # fixtures/children. Messages are NOT guaranteed, so reconcile against
        # TotalProcesses before confirming cleanup; a gap stays fail-closed.
        for _ in range(16384):
            message, key, value = W.DWORD(), C.c_size_t(), C.c_void_p()
            if not kernel.GetQueuedCompletionStatus(port, C.byref(message), C.byref(key), C.byref(value), 0):
                error_code = C.get_last_error()
                if error_code == 258:  # WAIT_TIMEOUT: queue drained.
                    return
                raise ProcessControlError('process_cleanup_unconfirmed', stage='job_events', winerror=error_code)
            if key.value != 1:
                raise ProcessControlError('process_cleanup_unconfirmed', stage='unknown_job_event')
            if message.value in {6, 7, 8} and value.value:  # NEW_PROCESS / EXIT / ABNORMAL_EXIT
                retain_member(value.value)
        raise ProcessControlError('process_tracking_limit_exceeded', event_limit=16384)

    def stop_job():
        remember_members()
        # Termination requests are asynchronous. Wait for every retained member,
        # not merely the launcher, an ACTIVE_PROCESS_ZERO notification or count.
        if not kernel.TerminateJobObject(job, 124):
            return False
        deadline = time.monotonic() + 5.0
        signaled = set()
        while True:
            drain_notifications()
            for member_pid, handle in list(members.items()):
                if member_pid in signaled:
                    continue
                left_ms = max(0, int((deadline - time.monotonic()) * 1000))
                if _winapi.WaitForSingleObject(handle, left_ms) != _winapi.WAIT_OBJECT_0:
                    cleanup_observation.update(unconfirmed_member_exit=True)
                    return False
                signaled.add(member_pid)
            after = accounting()
            cleanup_observation.update(
                retained_member_handles=len(members), removed_before_open=len(vanished),
                total_job_processes=int(after.TotalProcesses),
                member_exit_handles_signaled=len(signaled) == len(members),
                active_processes_after_cleanup=int(after.ActiveProcesses),
                tracking='job_pid_census_and_completion_notifications')
            if (after.ActiveProcesses == 0 and after.TotalProcesses == len(members) + len(vanished)
                    and len(signaled) == len(members)):
                return True
            if time.monotonic() >= deadline:
                return False
            # Late notifications may identify a child born immediately before
            # termination. Missing notifications never count as successful exit.
            time.sleep(.01)

    try:
        port = kernel.CreateIoCompletionPort(W.HANDLE(-1), None, 0, 1)
        if not port:
            raise ProcessControlError('process_containment_unavailable', stage='create_event_port', winerror=C.get_last_error())
        class CompletionPort(C.Structure):
            _fields_ = [('CompletionKey', C.c_void_p), ('CompletionPort', W.HANDLE)]
        association = CompletionPort(1, port)
        if not kernel.SetInformationJobObject(job, 7, C.byref(association), C.sizeof(association)):
            raise ProcessControlError('process_containment_unavailable', stage='associate_events', winerror=C.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)):
            raise ProcessControlError('process_containment_unavailable', stage='set_job_limits', winerror=C.get_last_error())
        current = _winapi.GetCurrentProcess()
        for file in (null, output, error):
            handles.append(_winapi.DuplicateHandle(current, msvcrt.get_osfhandle(file.fileno()),
                                                    current, 0, True, _winapi.DUPLICATE_SAME_ACCESS))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        startup.lpAttributeList = {'handle_list': handles}
        command = subprocess.list2cmdline(args)
        # Keep Python audit policy observable, in addition to _winapi's own event.
        sys.audit('subprocess.Popen', args[0], command, cwd, env)
        process, thread, pid, _ = _winapi.CreateProcess(
            args[0], command, None, None, True, subprocess.CREATE_NO_WINDOW | 0x00000004,
            env, cwd, startup)  # CREATE_SUSPENDED: no workload may start before assignment.
        for handle in handles:
            _winapi.CloseHandle(handle)
        handles.clear()
        if not _assign_windows_job(kernel, job, process):
            raise ProcessControlError('process_containment_unavailable', stage='assign_job',
                                      winerror=C.get_last_error(), workload_started=False)
        assigned = True
        remember_members()  # Includes the suspended root before any child can start.
        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
            raise ProcessControlError('process_launch_failed', stage='resume_thread', winerror=C.get_last_error())
        _winapi.CloseHandle(thread)
        thread = None
        timed_out = exceeded = False
        while _winapi.WaitForSingleObject(process, 20) != _winapi.WAIT_OBJECT_0:
            remember_members()
            if os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size > limit:
                exceeded = True
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                break
        code = _winapi.GetExitCodeProcess(process)
        confirmed = stop_job()  # Also reap asynchronous descendants on a normal root exit.
        root_exited = _winapi.WaitForSingleObject(process, 5000) == _winapi.WAIT_OBJECT_0
        if not confirmed or not root_exited:
            raise ProcessControlError('process_cleanup_unconfirmed', root_pid=pid, scope='windows_job',
                                      timed_out=timed_out, output_limit_exceeded=exceeded,
                                      **cleanup_observation)
        if timed_out:
            failure = WorkerTimeout(args, timeout, cleanup_confirmed=True, scope='windows_job')
            failure.details.update(cleanup_observation)
            raise failure
        if exceeded:
            raise ProcessControlError('worker_output_limit_exceeded', limit_bytes=limit, cleanup_confirmed=True,
                                      cleanup_scope='owned_process_execution', file_resources_released=None)
        return code, {'scope': 'windows_job', 'cleanup_confirmed': True,
                      'root_pid': pid, 'assignment_before_execution': True,
                      'root_exit_confirmed': root_exited, 'active_processes_after_cleanup': 0,
                      'cleanup_scope': 'owned_process_execution', 'file_resources_released': None,
                      **cleanup_observation}
    finally:
        # Never run an unassigned suspended child; it cannot have spawned descendants.
        if process is not None and not assigned:
            try:
                _winapi.TerminateProcess(process, 125)
                _winapi.WaitForSingleObject(process, 5000)
            except OSError:
                pass
        # Closing this non-inherited Job kills its members even during parent exit.
        kernel.CloseHandle(job)
        if port is not None:
            kernel.CloseHandle(port)
        if process is not None and assigned:
            _winapi.WaitForSingleObject(process, 5000)
        for handle in members.values():
            kernel.CloseHandle(handle)
        for handle in (*handles, thread, process):
            if handle is not None:
                _winapi.CloseHandle(handle)


def _posix(args, timeout, env, cwd, output, error, null, limit):
    # A dedicated session scopes signals to this invocation; no parent/host signals.
    process = subprocess.Popen(args, stdin=null, stdout=output, stderr=error, env=env,
                               cwd=cwd, start_new_session=True, close_fds=True)
    started = time.monotonic()
    timed_out = exceeded = False
    try:
        while process.poll() is None:
            if os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size > limit:
                exceeded = True
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                break
            time.sleep(0.02)
        code = process.poll()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    details = {'scope': 'posix_process_group', 'group_signal_delivered': True,
               'root_exit_confirmed': True, 'cleanup_confirmed': None,
               'cleanup_scope': 'owned_process_execution', 'file_resources_released': None,
               'note': 'POSIX group signalling is not a Windows Job active-process census.'}
    if timed_out:
        raise WorkerTimeout(args, timeout, cleanup_confirmed=None, scope='posix_process_group')
    if exceeded:
        raise ProcessControlError('worker_output_limit_exceeded', limit_bytes=limit, **details)
    return code, details


def run(args, *, timeout, env=None, cwd=None, max_output_bytes=16 * 1024 * 1024):
    """Run a known executable; UTF-8 text result, no shell/stdin/network policy changes.

    Only use for short workers that must not leave descendants running. Install
    resume workers intentionally survive the caller and MUST NOT use this API.
    The result has process_control metadata. A timeout is never ordinary success.
    Process exit is NOT a filesystem-unlock certificate: Windows can release
    byte-range locks later. Callers must acquire the real resource normally and
    fail closed if it remains busy; they must not remove lock files or retry work
    indefinitely. See Microsoft LockFile/LockFileEx remarks.
    """
    if not isinstance(args, (list, tuple)) or not args or not all(isinstance(x, (str, os.PathLike)) for x in args):
        raise ValueError('Expected a nonempty argv sequence')
    args = [os.fspath(x) for x in args]
    if not os.path.isabs(args[0]) or any('\0' in x for x in args):
        raise ValueError('Use an absolute executable path and NUL-free arguments')
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be a finite positive number')
    if not isinstance(max_output_bytes, int) or max_output_bytes < 1024:
        raise ValueError('max_output_bytes must be an integer >= 1024')
    cwd = os.fspath(cwd) if cwd is not None else None
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error, open(os.devnull, 'rb') as null:
        platform_run = _windows if os.name == 'nt' else _posix
        code, control = platform_run(args, timeout, env, cwd, output, error, null, max_output_bytes)
        if os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size > max_output_bytes:
            raise ProcessControlError('worker_output_limit_exceeded', limit_bytes=max_output_bytes, **control)
        result = subprocess.CompletedProcess(args, code, _decode(output, max_output_bytes), _decode(error, max_output_bytes))
        result.process_control = control
        return result
