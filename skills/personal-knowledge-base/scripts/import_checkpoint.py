"""Local per-file extraction checkpoints, not authority decisions or published releases."""
import hashlib
import json
import os
import uuid
from pathlib import Path
from import_progress import ImportProgress

class Checkpoint:
    def __init__(self, root, version):
        self.root = Path(root)
        self.version = version
        self.hits = 0
        self.progress = ImportProgress(self.root / "progress")

    def extract(self, path, digest, extractor):
        key = hashlib.sha256((digest + '\0' + self.version + '\0' + Path(path).suffix.lower()).encode()).hexdigest()
        target = self.root / (key + '.json')
        if target.is_file() and not target.is_symlink():
            try:
                # State files have a UTF-8 wire format, independent of the host locale.
                data = json.loads(target.read_text(encoding='utf-8'))
                body = data['body']
                signature = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if (signature == data['checksum'] and body['digest'] == digest and body['version'] == self.version
                        and body['meta'].get('complete_text_coverage', True)):
                    self.hits += 1
                    return body['text'], body['method'], body['meta']
            except (OSError, ValueError, KeyError, TypeError):
                pass
        text, method, meta = extractor(path)
        # Do not cache failed pages: repaired OCR must get a chance on retry.
        if meta.get('complete_text_coverage', True):
            self.root.mkdir(parents=True, exist_ok=True)
            body = {'digest': digest, 'version': self.version, 'text': text, 'method': method, 'meta': meta}
            data = {'body': body, 'checksum': hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
            temp = self.root / ('.' + uuid.uuid4().hex + '.tmp')
            try:
                temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        return text, method, meta
