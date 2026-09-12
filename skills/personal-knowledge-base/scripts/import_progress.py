"""Dedicated JSONL progress, never mixed with diagnostic stderr or source text."""
import json
from pathlib import Path
import time
import uuid


class ImportProgress:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = None
        self.run_id = uuid.uuid4().hex
        self.pass_number = 0
        self.total = 0

    def begin(self, total):
        if self.path is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path = self.directory / ('progress-' + self.run_id + '.jsonl')
            # Never truncate another operation or follow an existing log symlink.
            with self.path.open('x', encoding='utf-8'):
                pass
        self.pass_number += 1
        self.total = total
        self.record('processing', 0)

    def record(self, stage, processed, **fields):
        append(self.path, {'run_id': self.run_id, 'pass': self.pass_number,
                          'stage': stage, 'processed_files': processed,
                          'total_files': self.total, 'activation_complete': False, **fields})

    def describe(self):
        return {'path': str(self.path), 'format': 'jsonl', 'schema': 'personal-kb.import-progress.v1',
                'run_id': self.run_id, 'pass': self.pass_number,
                'diagnostics_channel': 'stderr', 'processed_is_not_adopted': True}


def append(path, fields):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise OSError('进度文件已变更或不可用')
    event = {'schema': 'personal-kb.import-progress.v1', 'time_unix': time.time(), **fields}
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
        handle.flush()


def finish(progress, result):
    if not progress:
        return
    append(progress['path'], {'run_id': progress['run_id'], 'pass': progress['pass'],
                             'stage': 'finished', 'status': result['status'],
                             'completion': result['completion'],
                             'activation_complete': bool(result['completion']['knowledge_data_ready'])})
