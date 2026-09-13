"""Common local material contracts; user content is data, never executable code."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import uuid

VERSION = 'agent-material-v1'
IMAGE_VERSION = 'image-vision-v1'
XLSX_VERSION = 'xlsx-structure-v1'
MAX_JSON = 32 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def safe(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('material_path_contains_link: use the canonical authorized path')
    return path


def read(path):
    path = safe(path)
    with path.open('rb') as stream: raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON: raise ValueError('material_json_limit')
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result: raise ValueError('duplicate_json_key')
            result[k] = v
        return result
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
        parse_constant=lambda v: (_ for _ in ()).throw(ValueError('nonfinite_json')))
    if not isinstance(value, dict): raise ValueError('material_json_object_required')
    return value


def write(path, value):
    path = safe(path); path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode('utf-8')
    if len(data) > MAX_JSON: raise ValueError('material_json_limit')
    tmp = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('xb') as stream:
        stream.write(data); stream.write(b'\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp, path)


def store_path(kb_root):
    root = Path(kb_root)
    return root.parent.parent / 'material-state' / root.name


def revision(kb_root):
    path = store_path(kb_root) / 'ledger.json'
    return file_hash(safe(path)) if path.exists() else 'none'
