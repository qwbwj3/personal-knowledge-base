"""Reuse immutable, hash-named managed snapshots; never link customer originals.

Links are storage deduplication, not independent backups. All normal readers retain
hash validation. Unsupported filesystems fall back to independent copies.
"""
import os
import re
import shutil
from pathlib import Path


def reuse_original(source: Path, destination: Path, expected_sha: str, hash_file):
    if (source.is_symlink() or not source.is_file()
            or not re.fullmatch(r'[0-9a-f]{64}\.[^/\\]+', source.name)
            or source.name.split('.')[0] != expected_sha or hash_file(source) != expected_sha):
        raise ValueError('原件快照身份或哈希不符，拒绝复用')
    try:
        os.link(source, destination)
        method = 'hardlink'
    except OSError:
        shutil.copy2(source, destination)
        method = 'copy_fallback'
    if destination.is_symlink() or hash_file(destination) != expected_sha:
        destination.unlink(missing_ok=True)
        raise ValueError('复用后原件快照完整性核验失败')
    return method
