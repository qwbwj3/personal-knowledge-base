"""Memory measurements of the calling worker, not its launcher or the whole machine."""
import os
import sys


def current_process_memory():
    result = {'peak_rss_bytes': None, 'memory_scope': 'current_worker_process',
              'memory_metric': None, 'memory_source': None, 'memory_unavailable_reason': None}
    try:
        if os.name == 'nt':
            import ctypes as C
            from ctypes import wintypes as W

            class Counters(C.Structure):
                _fields_ = [('cb', W.DWORD), ('PageFaultCount', W.DWORD)] + [
                    (name, C.c_size_t) for name in (
                        'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                        'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
                        'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]
            kernel = C.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], W.HANDLE
            api = C.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
            api.argtypes, api.restype = [W.HANDLE, C.POINTER(Counters), W.DWORD], W.BOOL
            counters = Counters()
            counters.cb = C.sizeof(counters)
            if not api(kernel.GetCurrentProcess(), C.byref(counters), counters.cb):
                raise C.WinError(C.get_last_error())
            result.update(peak_rss_bytes=int(counters.PeakWorkingSetSize),
                          current_working_set_bytes=int(counters.WorkingSetSize),
                          private_commit_bytes=int(counters.PrivateUsage),
                          memory_metric='peak_working_set', memory_source='GetProcessMemoryInfo')
        else:
            import resource
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result.update(peak_rss_bytes=int(value if sys.platform == 'darwin' else value * 1024),
                          memory_metric='peak_resident_set', memory_source='getrusage(RUSAGE_SELF)')
    except (OSError, ImportError, AttributeError) as exc:
        result['memory_unavailable_reason'] = type(exc).__name__ + ': ' + str(exc)
    return result
