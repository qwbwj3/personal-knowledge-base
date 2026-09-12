"""Quiet ordinary child processes on Windows; no shell, elevation or policy changes."""
import sys


def quiet_subprocess_kwargs():
    """Merge into subprocess.run/check_output/Popen for ordinary Git/Python children.

    Detached workers use their own DETACHED_PROCESS/BREAKAWAY flags instead.
    Output capture/stdio and timeouts remain the caller's responsibility.
    """
    return {'creationflags': 0x08000000} if sys.platform == 'win32' else {}
