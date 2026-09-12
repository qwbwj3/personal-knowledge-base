"""Verify the exact delivery inventory, without reading user data or networking.

This checks file identity, not publisher authenticity. Obtain the expected Git
commit / archive digest independently. Do not run untrusted downloaded code
merely to establish that code's own trustworthiness.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys

MANIFEST = 'RELEASE-MANIFEST.json'


def verify(root: Path) -> dict:
    manifest = root / MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('Missing or non-regular release manifest')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('schema') != 'personal-kb.release-inventory.v1':
        raise ValueError('Unsupported inventory schema')
    entries = data.get('files')
    if not isinstance(entries, dict) or not entries:
        raise ValueError('Empty or invalid inventory')
    actual = set()
    mismatches = []
    unsupported = []
    for base, directories, names in os.walk(root, followlinks=False):
        if Path(base) == root and '.git' in directories:
            directories.remove('.git')
        for name in list(directories):
            path = Path(base) / name
            if path.is_symlink():
                unsupported.append(path.relative_to(root).as_posix())
                directories.remove(name)
        for name in names:
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if Path(base) == root and name == '.git':
                # A linked Git worktree has a .git text pointer; it is not payload.
                continue
            if path.is_symlink() or not path.is_file():
                unsupported.append(relative)
                continue
            if relative == MANIFEST:
                continue
            actual.add(relative)
            expected = entries.get(relative)
            if expected is None:
                continue
            with path.open('rb') as stream:
                hasher = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(chunk)
                digest = hasher.hexdigest()
            if digest != expected.get('sha256') or path.stat().st_size != expected.get('bytes'):
                mismatches.append(relative)
            elif os.name != 'nt' and bool(path.stat().st_mode & 0o111) != expected.get('executable'):
                mismatches.append(relative)
    missing = sorted(set(entries) - actual)
    unexpected = sorted(actual - set(entries))
    success = not (missing or unexpected or mismatches or unsupported)
    return {'status': 'verified' if success else 'rejected',
            'version': data.get('version'), 'checked_files': len(actual),
            'missing_files': missing, 'unexpected_files': unexpected,
            'mismatched_files': sorted(mismatches), 'unsupported_paths': sorted(unsupported),
            'publisher_authenticity_verified': False}


def main() -> int:
    try:
        result = verify(Path(__file__).resolve().parents[1])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # No absolute paths, environment dumps, or original data in the report.
        result = {'status': 'rejected', 'error_type': type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'verified' else 2


if __name__ == '__main__':
    raise SystemExit(main())
