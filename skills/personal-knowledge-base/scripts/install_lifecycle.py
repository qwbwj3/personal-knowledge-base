"""Git-anchored, target-locked code lifecycle. Never manages knowledge data."""
from __future__ import annotations
import argparse
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid
from installer_process import quiet_subprocess_kwargs

ORIGINS = {'https://github.com/qwbwj3/personal-knowledge-base.git',
           'https://github.com/qwbwj3/personal-knowledge-base',
           'git@github.com:qwbwj3/personal-knowledge-base.git',
           'ssh://git@github.com/qwbwj3/personal-knowledge-base.git'}
# Old receipts remain readable; their commits must still be verified ancestors.
# The old URL is NOT accepted as a source for new installation requests.
RECEIPT_ORIGINS = ('qwbwj3/personal-knowledge-base', 'qwbwj3/personal-knowledge-base-test')
PREFIX = 'skills/personal-knowledge-base'

def activation_denied(exc):
    """Only a denied directory switch, not arbitrary I/O or a guessed process."""
    return (isinstance(exc, OSError) and hasattr(exc, 'install_move') and
            (getattr(exc, 'winerror', None) in {5, 32, 33} or
             exc.errno in {errno.EACCES, errno.EPERM, errno.EBUSY, errno.ETXTBSY}))

def blocker_details(exc):
    return {'kind': 'access_or_sharing_denied', 'errno': exc.errno,
            'winerror': getattr(exc, 'winerror', None),
            'operation': 'directory_move', 'paths': exc.install_move,
            'occupying_process_verified': False}

class Refused(Exception):
    def __init__(self, reason, advice):
        self.reason, self.advice = reason, advice
        super().__init__(reason)

def refuse(reason, advice='请让宿主Agent检查来源、路径和文件；保留现有安装，不要手动删除或修改收据。'):
    raise Refused(reason, advice)

def safe(path):
    path = Path(os.path.abspath(Path(path).expanduser()))
    for item in [path, *path.parents]:
        if item.is_symlink():
            refuse('symlink_path')
    return path

def git(repo, *args, input=None):
    try:
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE, input=input, timeout=120, **quiet_subprocess_kwargs())
    except FileNotFoundError:
        refuse('git_missing', '请宿主Agent通过正常授权准备Git；不会自动sudo或全局安装。')
    except subprocess.TimeoutExpired:
        refuse('git_verification_timeout', 'Git校验超时，未授权切换；请检查本机Git和磁盘，勿跳过校验。')
    except subprocess.CalledProcessError:
        refuse('git_verification_failed', '请使用已授权GitHub仓库的正常克隆，并确认所选完整提交存在。')

def git_blobs(repo, oids):
    """One strict cat-file batch, binding each byte string to its expected Git OID."""
    if not all(re.fullmatch('[0-9a-f]{40}', oid) for oid in oids):
        refuse('invalid_git_blob_oid')
    if not oids:
        return []
    payload = git(repo, 'cat-file', '--batch', input=('\n'.join(oids) + '\n').encode('ascii'))
    cursor, result = 0, []
    for oid in oids:
        newline = payload.find(b'\n', cursor)
        if newline < 0:
            refuse('invalid_git_batch_header')
        header = payload[cursor:newline]
        fields = header.split(b' ')
        if (len(fields) != 3 or fields[0] != oid.encode('ascii') or fields[1] != b'blob'
                or not re.fullmatch(b'0|[1-9][0-9]*', fields[2])):
            refuse('invalid_git_batch_header')
        # Reject absurd integer strings before conversion, then compare to buffer.
        if len(fields[2]) > 12:
            refuse('invalid_git_batch_size')
        size = int(fields[2])
        begin, end = newline + 1, newline + 1 + size
        if end >= len(payload) or payload[end:end + 1] != b'\n':
            refuse('truncated_git_batch_blob')
        data = payload[begin:end]
        actual = hashlib.sha1(b'blob ' + str(size).encode('ascii') + b'\0' + data).hexdigest()
        if actual != oid:
            refuse('git_batch_blob_hash_mismatch')
        result.append(data)
        cursor = end + 1
    if cursor != len(payload):
        refuse('unexpected_git_batch_trailer')
    return result

def inventory(root):
    result = {}
    if not root.is_dir() or root.is_symlink():
        refuse('not_regular_directory')
    def walk_error(exc):
        exc.install_operation = 'enumerate_payload_directory'
        exc.install_path = getattr(exc, 'filename', str(root))
        raise exc
    for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs + files:
            p = Path(base) / name
            try:
                mode = p.lstat().st_mode
            except OSError as exc:
                exc.install_operation, exc.install_path = 'stat_payload_entry', str(p)
                raise
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                refuse('unsupported_payload_type')
        for name in files:
            p = Path(base) / name
            try:
                result[p.relative_to(root).as_posix()] = (hashlib.sha256(p.read_bytes()).hexdigest(), bool(p.stat().st_mode & 0o111))
            except OSError as exc:
                exc.install_operation = 'read_payload_file'
                exc.install_path = str(p)
                raise
    return result

class Anchor:
    def __init__(self, source, expected=None):
        self.cache = {}
        self.blob_cache = {}
        self.source = safe(source)
        if not self.source.is_dir():
            refuse('unanchored_archive', '散包/ZIP不作为安装入口。请从已授权GitHub克隆运行；包内manifest不能证明来源。')
        self.repo = safe(git(self.source, 'rev-parse', '--show-toplevel').decode().strip())
        if self.source != self.repo / PREFIX:
            refuse('unexpected_payload_location')
        origin = git(self.repo, 'remote', 'get-url', 'origin').decode().strip()
        if origin not in ORIGINS:
            refuse('unauthorized_origin', '请从 qwbwj3/personal-knowledge-base 正常授权克隆；不接受本地路径或其他仓库作为origin。')
        self.commit = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        if expected and (not re.fullmatch('[0-9a-f]{40}', expected) or expected != self.commit):
            refuse('expected_commit_mismatch', '请切换到经用户确认的完整40位提交后再运行。')
        if git(self.repo, 'diff', '--cached', '--name-only', self.commit, '--', PREFIX).strip():
            refuse('dirty_index')
        self.manifest(self.commit)
        self.verify(self.source, self.commit)

    def manifest(self, commit):
        if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
            refuse('invalid_receipt_commit')
        # Cached manifests have already passed the ancestry check in this Anchor.
        if commit in self.cache:
            return self.cache[commit]
        # A receipt cannot authorize an unrelated/dangling local Git object.
        git(self.repo, 'merge-base', '--is-ancestor', commit, self.commit)
        rows = git(self.repo, 'ls-tree', '-rz', commit, '--', PREFIX)
        result, entries = {}, []
        for row in rows.split(b'\0'):
            if not row:
                continue
            meta, name = row.split(b'\t', 1)
            mode, kind, oid = meta.decode().split()
            relative = name.decode().removeprefix(PREFIX + '/')
            if mode not in ('100644', '100755') or kind != 'blob' or relative.startswith('/') or '..' in Path(relative).parts:
                refuse('unsupported_git_payload')
            if not name.decode().startswith(PREFIX + '/') or relative in {x[0] for x in entries}:
                refuse('unsupported_git_payload')
            entries.append((relative, mode, oid))
        blobs = git_blobs(self.repo, [entry[2] for entry in entries])
        data_map = {}
        for (relative, mode, oid), data in zip(entries, blobs):
            data_map[relative] = data
            result[relative] = (hashlib.sha256(data).hexdigest(), mode == '100755')
        if not {'PACKAGE.json', 'SKILL.md', 'scripts/personal_kb.py'}.issubset(result):
            refuse('missing_git_payload')
        self.blob_cache[commit] = data_map
        self.cache[commit] = result
        return result

    def verify(self, root, commit):
        actual, expected = inventory(root), self.manifest(commit)
        # Windows does not expose Git's executable bit through stat.
        if os.name == 'nt':
            actual = {k: (v[0], expected.get(k, ('', False))[1]) for k, v in actual.items()}
        if actual != expected:
            refuse('payload_mismatch', '文件集合、内容或执行模式与所选Git提交不同；请使用干净克隆，勿删除人工文件来强行通过。')

    def copy(self, target, commit):
        target.mkdir()
        for relative, (_, executable) in self.manifest(commit).items():
            p = target / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                with p.open('wb') as f:
                    f.write(self.blob_cache[commit][relative])
                    f.flush()
                    os.fsync(f.fileno())
                p.chmod(0o755 if executable else 0o644)
            except OSError as exc:
                exc.install_operation, exc.install_path = 'copy_verified_payload_file', str(p)
                raise
        for base, _, _ in os.walk(target, topdown=False):
            sync_dir(Path(base))
        self.verify(target, commit)

def write_json(path, value):
    safe(path)
    tmp = safe(path.with_suffix('.tmp'))
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        exc.install_operation, exc.install_path = 'write_management_record', str(tmp)
        raise
    try:
        os.replace(tmp, path)
        sync_dir(path.parent)
    except OSError as exc:
        exc.install_operation, exc.install_path = 'publish_management_record', str(path)
        raise

def sync_dir(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

def read_json(path):
    safe(path)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except PermissionError as exc:
        exc.install_operation, exc.install_path = 'read_management_record', str(path)
        raise
    except (ValueError, OSError):
        refuse('invalid_management_record')

@contextlib.contextmanager
def lock(path):
    safe(path)
    with path.open('a+b') as f:
        try:
            if os.name == 'nt':
                import msvcrt
                f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            refuse('target_busy', '同一目标已有安装操作，请稍后重试；不会终止其他程序。')
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)

def capability_probe(target):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-B', str(target / 'scripts/personal_kb.py'), 'platform-check']
    try:
        cp = subprocess.run(command, text=True, capture_output=True, timeout=60, env=env, cwd=target, **quiet_subprocess_kwargs())
        value = json.loads(cp.stdout[cp.stdout.index('{'):])
        return {'returncode': cp.returncode, 'result': value}
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return {'returncode': 2, 'error': type(exc).__name__, 'operation': 'candidate_capability_probe', 'path': getattr(exc, 'filename', None), 'winerror': getattr(exc, 'winerror', None)}

def capability_ready(probe):
    runtime = (probe.get('result') or {}).get('native_runtime') or {}
    basic = bool((runtime.get('text_html_docx_xlsx') or {}).get('ready'))
    pdf = bool((runtime.get('pdf_text') or {}).get('ready'))
    # PDF is a core input on every OS. OCR is an explicit, lazy supplement;
    # missing optional models must not block an otherwise verified install.
    return probe.get('returncode') == 0 and basic and pdf

class Lifecycle:
    def __init__(self, anchor, target, fault=None):
        self.anchor, self.target, self.fault = anchor, safe(target), fault
        source = anchor.source
        if self.target == source or source in self.target.parents or self.target in source.parents or self.target == Path(self.target.anchor):
            refuse('overlapping_or_unsafe_target')
        if self.target.exists() and not self.target.is_dir():
            refuse('not_regular_directory')
        self.manager = safe(self.target.parent / ('.' + self.target.name + '.install-manager'))
        if self.manager.exists() and not self.manager.is_dir():
            refuse('invalid_management_directory')

    def version(self, ident):
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
            refuse('invalid_version_id')
        return safe(self.manager / ident)

    def receipt(self, ident):
        value = read_json(self.version(ident) / 'receipt.json')
        if not isinstance(value, dict) or value.get('id') != ident or value.get('origin') not in RECEIPT_ORIGINS:
            refuse('invalid_receipt')
        self.anchor.manifest(value.get('commit'))
        return value

    def active(self):
        path = self.manager / 'active.json'
        if not path.exists():
            return None
        value = read_json(path)
        if not isinstance(value, dict) or set(value) != {'id'}:
            refuse('invalid_management_record')
        if value['id'] is not None:
            self.version(value['id'])
        return value['id']

    def move(self, source, target):
        if target.exists():
            refuse('occupied_transaction_destination')
        try:
            os.replace(source, target)
        except OSError as exc:
            exc.install_move = {'source': str(source), 'target': str(target)}
            exc.install_operation, exc.install_path = 'directory_move', str(source)
            raise
        sync_dir(source.parent); sync_dir(target.parent)

    def transaction(self, old, new, phase, blocker=None):
        # A permanent journal, atomically replaced; deletion is never a commit.
        record = {'schema': 'personal-kb.transaction.v2', 'old': old, 'new': new, 'phase': phase}
        if blocker is not None:
            record['blocker'] = blocker
        write_json(self.manager / 'transaction.json', record)
        self.trip('journal_' + phase)

    def journal(self):
        txn = self.manager / 'transaction.json'
        if not txn.exists():
            return None
        record = read_json(txn)
        phases = {'prepared', 'moving_old', 'moving_new', 'committing',
                  'committed', 'rolling_back', 'aborted', 'awaiting_release'}
        if not isinstance(record, dict):
            refuse('invalid_management_record')
        legacy = set(record) == {'old', 'new'}
        if not legacy:
            expected = {'schema', 'old', 'new', 'phase'}
            if record.get('phase') == 'awaiting_release':
                expected.add('blocker')
            if (set(record) != expected or
                    record.get('schema') not in {'personal-kb.transaction.v1', 'personal-kb.transaction.v2'} or
                    not isinstance(record.get('phase'), str) or record['phase'] not in phases):
                refuse('invalid_management_record')
            if record['phase'] == 'awaiting_release' and (
                    record['schema'] != 'personal-kb.transaction.v2' or
                    not isinstance(record['blocker'], dict) or
                    record['blocker'].get('kind') != 'access_or_sharing_denied'):
                refuse('invalid_management_record')
        old, new = record['old'], record['new']
        self.version(new)
        if old is not None:
            self.version(old)
        if old == new:
            refuse('ambiguous_transaction')
        return record

    def recover(self):
        record = self.journal()
        if record is None:
            return False
        old, new = record['old'], record['new']
        legacy = set(record) == {'old', 'new'}
        phase = record.get('phase')
        active = self.active()
        # Terminal records validate the active installation, not historical
        # backups (status reports damaged backups without breaking the active code).
        if phase in {'committed', 'aborted'}:
            expected = new if phase == 'committed' else old
            if active != expected:
                refuse('ambiguous_transaction')
            if expected:
                self.anchor.verify(self.target, self.receipt(expected)['commit'])
            elif self.target.exists():
                refuse('ambiguous_transaction')
            return False
        nr = self.receipt(new)
        new_code = self.version(new) / 'code'
        old_code = self.version(old) / 'code' if old else None
        self.storage_filesystem(self.version(new),
                                *([self.version(old)] if old else []),
                                *[p for p in (self.target, new_code, old_code) if p is not None and p.exists()])
        if active not in (old, new):
            refuse('ambiguous_transaction')
        layout = (self.target.exists(), new_code.exists(), bool(old and old_code.exists()))
        before = (bool(old), True, False)
        between = (False, True, bool(old))
        after = (True, False, bool(old))
        if phase == 'awaiting_release':
            # A fully verified candidate is waiting; status must not repeatedly
            # rename the active directory just because the host asks for status.
            if active != old or layout != before:
                refuse('ambiguous_transaction')
            if old:
                self.anchor.verify(self.target, self.receipt(old)['commit'])
            self.anchor.verify(new_code, nr['commit'])
            return False
        allowed = {'prepared': {before}, 'moving_old': {before, between},
                   'moving_new': {between, after}, 'committing': {after},
                   'rolling_back': {before, between, after}}
        if not legacy and (layout not in allowed[phase] or
                           (phase in {'prepared', 'moving_old', 'moving_new'} and active != old)):
            refuse('ambiguous_transaction')
        if phase == 'committing':
            # The commit decision was durable before publishing active.json.
            if new_code.exists() or (old and not old_code.exists()):
                refuse('ambiguous_transaction')
            self.anchor.verify(self.target, nr['commit'])
            if old:
                self.anchor.verify(old_code, self.receipt(old)['commit'])
            write_json(self.manager / 'active.json', {'id': new})
            self.transaction(old, new, 'committed')
            return True
        # Validate every candidate and backup before the first rollback mutation.
        target_is_new = self.target.exists() and (not old or old_code.exists())
        if old:
            self.anchor.verify(old_code if old_code.exists() else self.target,
                               self.receipt(old)['commit'])
        if target_is_new:
            if new_code.exists():
                refuse('ambiguous_transaction')
            self.anchor.verify(self.target, nr['commit'])
        elif new_code.exists():
            self.anchor.verify(new_code, nr['commit'])
        else:
            refuse('missing_candidate')
        self.transaction(old, new, 'rolling_back')
        if target_is_new:
            self.move(self.target, new_code)
        self.trip('recovery_new_stored')
        if old and old_code.exists():
            self.move(old_code, self.target)
        self.trip('recovery_old_restored')
        write_json(self.manager / 'active.json', {'id': old})
        self.trip('recovery_active_written')
        self.transaction(old, new, 'aborted')
        return True

    def resume_command(self, ident):
        # Structured argv: the host quotes for its shell, no shell interpolation.
        return [sys.executable, '-B', str(self.anchor.source / 'scripts/install_workbuddy.py'),
                '--resume', ident, '--source', str(self.anchor.source),
                '--target', str(self.target), '--expected-commit', self.anchor.commit,
                '--wait-seconds', '120']

    def pending_result(self, record, *, recovery=False, blocker=None, recovered=False):
        old, new = record['old'], record['new']
        active = self.receipt(old) if old and not recovery else None
        receipt = self.receipt(new)
        return {
            'status': 'recovery_pending' if recovery else 'activation_pending',
            'target': str(self.target), 'active': active,
            'active_code_verified': not recovery and bool(old),
            'requested_commit': self.anchor.commit,
            'version_matches_request': False,
            'pending': {'id': new, 'commit': receipt['commit'],
                        'phase': record.get('phase'), 'candidate_reused_on_retry': True},
            'blocker': blocker or record.get('blocker'),
            'completion': {'complete': False, 'activation_complete': False},
            'impact': ('恢复尚未完成，暂勿调用Skill；旧版和候选保留，不能声称旧版已恢复。' if recovery else
                       ('新版已验证暂存，尚未生效；原有版本保留。不修改知识库或原资料。' if old else
                        '候选已验证暂存，但首次安装尚未生效。没有可调用的旧安装，不修改知识库或原资料。')),
            'next_step': ('请保存工作，通过正常方式退出占用程序（可能是WorkBuddy，尚未定位具体进程）。'
                          'Windows宿主Agent可先运行resume_detached_argv启动有界独立续装，确认worker独立就绪后请用户正常退出；若启动被拒，再由独立终端运行resume_argv。'
                          '不要在退出WorkBuddy时会一起结束的任务进程中等待，不强制终止进程、不关闭安全保护。'
                          '若释放后仍拒绝，请检查目录权限/安全软件，勿反复复制、覆盖文件或改收据。'),
            'resume_argv': self.resume_command(new),
            'resume_detached_argv': [sys.executable, '-B', str(self.anchor.source / 'scripts/install_workbuddy.py'),
                '--resume-detached', new, '--source', str(self.anchor.source),
                '--target', str(self.target), '--expected-commit', self.anchor.commit],
            'recovered': recovered,
        }, 3

    def reusable_candidate(self, commit, old):
        """Reuse verified retained candidates, including rc.3 aborted attempts."""
        journal = self.journal()
        self.retained_incomplete_candidates = []
        for path in sorted(self.manager.iterdir()):
            if not re.fullmatch('[0-9a-f]{32}', path.name) or path.name == old:
                continue
            try:
                raw = read_json(path / 'receipt.json')
            except Refused:
                # Unrelated damaged/incomplete receipts are diagnostic records,
                # never candidates. Keep them without blocking every future upgrade.
                continue
            if not isinstance(raw, dict) or raw.get('commit') != commit:
                continue
            receipt = self.receipt(path.name)
            candidate = self.version(path.name) / 'code'
            if not candidate.exists():
                continue
            self.storage_filesystem(path, candidate)
            try:
                self.anchor.verify(candidate, receipt['commit'])
            except Refused as exc:
                # Copy can crash before verification or a transaction is started.
                # That unprepared material is never executed/reused. Keep it and
                # prepare a fresh Git copy; do NOT treat a corrupted prepared or
                # journal-bound candidate this way (those must fail closed).
                prepared = self.version(path.name) / 'prepared.json'
                if (exc.reason != 'payload_mismatch' or prepared.exists() or
                        (journal and path.name in {journal['old'], journal['new']})):
                    raise
                self.retained_incomplete_candidates.append(path.name)
                continue
            return path.name
        return None

    def mark_prepared(self, ident, commit):
        marker = self.version(ident) / 'prepared.json'
        expected = {'schema': 'personal-kb.prepared-candidate.v1', 'commit': commit}
        if marker.exists():
            if read_json(marker) != expected:
                refuse('invalid_prepared_record')
        else:
            write_json(marker, expected)

    def trip(self, point):
        # In-process injection only; no production environment-variable backdoor.
        if self.fault:
            self.fault(point)

    def storage_filesystem(self, *paths):
        device = self.target.parent.stat().st_dev
        for path in (self.manager, *paths):
            if safe(path).stat().st_dev != device:
                refuse('different_filesystems', '管理目录、候选、备份与安装目标须在同一文件系统；源Git克隆可以位于其他磁盘。')

    def version_details(self, active):
        details = []
        for path in sorted(self.manager.iterdir()):
            if not re.fullmatch('[0-9a-f]{32}', path.name):
                continue
            row = {'id': path.name, 'commit': None, 'active': path.name == active,
                   'record_status': 'invalid', 'code_status': 'not_checked',
                   'capability_checked': False}
            try:
                # Claimed commit is diagnostic only until receipt validation succeeds.
                raw = read_json(self.version(path.name) / 'receipt.json')
                if isinstance(raw, dict) and isinstance(raw.get('commit'), str):
                    row['commit'] = raw['commit']
                receipt = self.receipt(path.name)
                row['record_status'] = 'valid'
                code = self.target if row['active'] else path / 'code'
                if not code.exists() and not code.is_symlink():
                    row.update(code_status='missing', message='代码缺失，不能回滚。')
                else:
                    row['code_status'] = 'invalid'
                    self.storage_filesystem(path, code)
                    self.anchor.verify(code, receipt['commit'])
                    row.update(code_status='verified', message='记录与代码已验证；回滚仍须能力预检。' if not row['active'] else '当前代码已验证。')
            except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                row.update(reason=exc.reason if isinstance(exc, Refused) else type(exc).__name__,
                           message='记录或代码未通过验证，不能据此回滚；保留文件供宿主排查。')
            details.append(row)
        return details

    def switch(self, old, new):
        self.storage_filesystem(self.version(new), self.version(new) / 'code',
                                *([self.version(old), self.target] if old else []))
        if old and (self.version(old) / 'code').exists():
            # A manually copy-activated rc.3 can leave the complete staging copy
            # beside the active directory. Accept only an exact duplicate; keep
            # it under a non-reserved name instead of deleting or trusting it.
            duplicate = self.version(old) / 'code'
            self.anchor.verify(duplicate, self.receipt(old)['commit'])
            self.move(duplicate, self.version(old) / ('retained-active-copy-' + uuid.uuid4().hex))
        self.transaction(old, new, 'prepared')
        self.trip('prepared')
        self.transaction(old, new, 'moving_old')
        if old:
            self.move(self.target, self.version(old) / 'code')
        self.trip('old_moved')
        self.transaction(old, new, 'moving_new')
        self.move(self.version(new) / 'code', self.target)
        self.trip('new_moved')
        self.transaction(old, new, 'committing')
        write_json(self.manager / 'active.json', {'id': new})
        self.trip('receipt_written')
        self.transaction(old, new, 'committed')

    def run(self, action, rollback_id=None, adopt_commit=None, resume_id=None, cancel_id=None):
        if action in {'resume', 'cancel_pending'}:
            self.version(resume_id if action == 'resume' else cancel_id)
            if not self.manager.exists():
                refuse('pending_activation_required')
            initial = self.journal()
            selected = resume_id if action == 'resume' else cancel_id
            if not initial or initial['new'] != selected:
                refuse('pending_activation_required', '请使用当前status返回的待切换ID；不猜ID或修改管理记录。')
        if action in ('check', 'status') and not self.manager.exists():
            if self.target.exists():
                refuse('unmanaged_installation', '现有目录无收据；可提供已受信旧提交使用 --adopt-commit 精确接管，或由用户另选安装路径。')
            return {'status': 'needs_install', 'target': str(self.target)}, 1
        self.target.parent.mkdir(parents=True, exist_ok=True)
        safe(self.manager)
        # Preserve host interception, but avoid its nonrecursive exist_ok bug
        # when this already-validated management directory needs no creation.
        if not self.manager.is_dir():
            self.manager.mkdir(exist_ok=True)
        safe(self.manager)
        if self.manager.stat().st_dev != self.target.parent.stat().st_dev or (self.target.exists() and self.target.stat().st_dev != self.manager.stat().st_dev):
            refuse('different_filesystems')
        with lock(self.manager / 'lock'):
            try:
                recovered = self.recover()
            except OSError as exc:
                if activation_denied(exc):
                    return self.pending_result(self.journal(), recovery=True, blocker=blocker_details(exc))
                raise
            journal = self.journal()
            old = self.active()
            if old:
                self.anchor.verify(self.target, self.receipt(old)['commit'])
            elif self.target.exists():
                if not adopt_commit or action not in ('apply', 'update'):
                    refuse('unmanaged_installation', '使用 --adopt-commit <受信旧提交> 精确核对后接管；不匹配则保留目录，请用户选择其他路径。')
                self.anchor.verify(self.target, adopt_commit)
                old = self.prepare_receipt(adopt_commit)
                write_json(self.manager / 'active.json', {'id': old})
            pending = journal if journal and journal.get('phase') == 'awaiting_release' else None
            if action in {'resume', 'cancel_pending'}:
                selected = resume_id if action == 'resume' else cancel_id
                # Recheck under the target lock, after any crash recovery.
                if not journal or journal['new'] != selected:
                    refuse('pending_activation_required')
                if journal.get('phase') == 'committed' and old == selected and action == 'resume':
                    self.require_capability(self.target)
                    return {'status': 'ready', 'idempotent': True, 'active': self.receipt(old),
                            'requested_commit': self.anchor.commit,
                            'version_matches_request': self.receipt(old)['commit'] == self.anchor.commit,
                            'completion': {'complete': True, 'activation_complete': True},
                            'target': str(self.target), 'recovered': recovered}, 0
                if journal.get('phase') not in {'awaiting_release', 'aborted'} or journal['old'] != old:
                    refuse('pending_activation_required')
                self.anchor.verify(self.version(selected) / 'code', self.receipt(selected)['commit'])
                if action == 'cancel_pending':
                    self.transaction(old, selected, 'aborted')
                    return {'status': 'pending_cancelled', 'target': str(self.target),
                            'active': self.receipt(old) if old else None,
                            'retained_candidate': selected, 'files_deleted': False,
                            'recovered': recovered}, 0
            backups = []
            for p in sorted(self.manager.iterdir()):
                if re.fullmatch('[0-9a-f]{32}', p.name) and (p / 'code').exists():
                    backups.append(p.name)
            if action == 'check' and old:
                self.require_capability(self.target)
            if action in ('check', 'status'):
                if pending:
                    return self.pending_result(pending, recovered=recovered)
                return {'status': 'ready' if old else 'needs_install', 'target': str(self.target), 'active': self.receipt(old) if old else None, 'stored_versions': backups, 'version_details': self.version_details(old), 'recovered': recovered,
                        'requested_commit': self.anchor.commit,
                        'version_matches_request': bool(old and self.receipt(old)['commit'] == self.anchor.commit)}, 0 if old else 1
            if pending and action != 'resume':
                same = (action in {'apply', 'update'} and self.receipt(pending['new'])['commit'] == self.anchor.commit)
                same = same or (action == 'rollback' and pending['new'] == rollback_id)
                if not same:
                    refuse('pending_activation_conflict', '已有不同版本等待切换。请先resume该ID，或经用户决定执行--cancel-pending <该ID>保留并取消，再更新；不创建更多候选。')
            reused = False
            if action == 'resume' or pending:
                new = resume_id if action == 'resume' else pending['new']
                receipt = self.receipt(new)
                candidate = self.version(new) / 'code'
                self.anchor.verify(candidate, receipt['commit'])
                reused = True
            elif action == 'rollback':
                if not old or not rollback_id or rollback_id == old:
                    refuse('rollback_id_required', '先运行 --status，再用 --rollback <备份ID> 选择代码备份。')
                new = rollback_id
                receipt = self.receipt(new)
                candidate = self.version(new) / 'code'
                self.anchor.verify(candidate, receipt['commit'])
            else:
                if old and self.receipt(old)['commit'] == self.anchor.commit:
                    self.require_capability(self.target)
                    return {'status': 'ready', 'target': str(self.target), 'idempotent': True, 'recovered': recovered}, 0
                new = self.reusable_candidate(self.anchor.commit, old)
                reused = new is not None
                new = new or self.prepare_receipt(self.anchor.commit)
                candidate = self.version(new) / 'code'
                if not reused:
                    try:
                        self.anchor.copy(candidate, self.anchor.commit)
                    except Exception:
                        # Retain failed staging for diagnosis; successful or
                        # failed activation never depends on a delete hook.
                        raise
                receipt = self.receipt(new)
            self.storage_filesystem(self.version(new), candidate)
            self.anchor.verify(candidate, receipt['commit'])
            self.mark_prepared(new, receipt['commit'])
            probe = capability_probe(candidate)
            if not capability_ready(probe):
                refuse('capability_problem', '候选未生效，旧版保留。请宿主Agent通过正常授权准备缺失的格式依赖后重试。诊断：' + json.dumps(probe, ensure_ascii=False))
            self.anchor.verify(candidate, receipt['commit'])
            # Catch changes made while the candidate was being probed.
            if old:
                self.anchor.verify(self.target, self.receipt(old)['commit'])
            try:
                self.switch(old, new)
            except Exception as exc:
                try:
                    self.recover()
                except OSError as recovery_error:
                    if activation_denied(recovery_error):
                        return self.pending_result(self.journal(), recovery=True, blocker=blocker_details(recovery_error))
                    raise
                record = self.journal()
                if activation_denied(exc) and record and record['old'] == old and record['new'] == new:
                    self.transaction(old, new, 'awaiting_release', blocker_details(exc))
                    return self.pending_result(self.journal(), recovered=recovered)
                raise
            # Re-validate the activated payload, not only its receipt or staging.
            self.anchor.verify(self.target, receipt['commit'])
            return {'status': 'activated' if action == 'resume' else ('rolled_back' if action == 'rollback' else ('updated' if old else 'installed')), 'target': str(self.target), 'active': new, 'backup': old, 'capabilities': probe, 'recovered': recovered,
                    'candidate_reused': reused, 'requested_commit': self.anchor.commit,
                    'retained_incomplete_candidates': getattr(self, 'retained_incomplete_candidates', []),
                    'activated_commit': receipt['commit'],
                    'version_matches_request': receipt['commit'] == self.anchor.commit,
                    'completion': {'complete': True, 'activation_complete': True},
                    'host_reload_required': bool(old),
                    'host_loaded_version_verified': False}, 0

    def require_capability(self, target):
        probe = capability_probe(target)
        if not capability_ready(probe):
            refuse('capability_problem', '代码完整性已验证，但当前宿主格式依赖不满足；请宿主Agent通过正常授权准备依赖。诊断：' + json.dumps(probe, ensure_ascii=False))
        self.anchor.verify(target, self.receipt(self.active())['commit'])
        return probe

    def prepare_receipt(self, commit):
        ident = uuid.uuid4().hex
        self.version(ident).mkdir()
        write_json(self.version(ident) / 'receipt.json', {'schema': 'personal-kb.install.v2', 'id': ident, 'commit': commit, 'origin': 'qwbwj3/personal-knowledge-base'})
        return ident

def run_bounded(lifecycle, action, wait_seconds, *, progress=None, **kwargs):
    """Verify normally on real attempts; probe known move blockers cheaply between them.

    Backoff bounds retries even if a directory-open probe cannot detect a child
    handle. No blanket PermissionError becomes pending: only run's tagged rename
    failures return code 3. Deadline is checked before each new expensive attempt.
    """
    from install_detached import directory_release_probe
    deadline = time.monotonic() + wait_seconds
    delay = 5
    result, code = lifecycle.run(action, **kwargs)
    while code == 3 and action == 'resume' and wait_seconds:
        if progress:
            progress('waiting', blocker=result.get('blocker'), complete=False)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result['wait_timed_out'] = True
            break
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 30)
        if time.monotonic() >= deadline:
            result['wait_timed_out'] = True
            break
        # A false result proves that this directory is still access/share denied;
        # unknown is not authority to skip the transaction's full verification.
        if directory_release_probe(result.get('blocker')) is False:
            continue
        if progress:
            progress('verifying', complete=False)
        result, code = lifecycle.run(action, **kwargs)
    return result, code

def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ('check', 'apply', 'update', 'status'):
        mode.add_argument('--' + name, action='store_true')
    mode.add_argument('--rollback', metavar='BACKUP_ID')
    mode.add_argument('--resume', metavar='CANDIDATE_ID')
    mode.add_argument('--resume-detached', metavar='CANDIDATE_ID')
    parser.add_argument('--detached-worker-id', help=argparse.SUPPRESS)
    mode.add_argument('--cancel-pending', metavar='CANDIDATE_ID')
    parser.add_argument('--wait-seconds', type=float,
                        help='Only with --resume; foreground bounded wait (0..300 seconds), never kills a host')
    parser.add_argument('--source', default=str(Path(__file__).absolute().parents[1]))
    parser.add_argument('--target')
    parser.add_argument('--expected-commit')
    parser.add_argument('--adopt-commit')
    args = parser.parse_args()
    if args.wait_seconds is not None and (not 0 <= args.wait_seconds <= 300 or not args.resume):
        parser.error('--wait-seconds requires --resume and a finite value from 0 to 300')
    if args.detached_worker_id and (not args.resume or args.wait_seconds != 300):
        parser.error('--detached-worker-id is internal and requires bounded --resume')
    args.wait_seconds = args.wait_seconds or 0
    config = os.environ.get('WORKBUDDY_CONFIG_DIR') or os.environ.get('CODEBUDDY_CONFIG_DIR') or str(Path.home() / '.codebuddy')
    target = args.target or str(Path(config).expanduser() / 'skills/personal-knowledge-base')
    stage = 'worker_startup'
    progress = None
    guard = contextlib.ExitStack()
    try:
        if args.detached_worker_id:
            from install_detached import early_worker
            progress = guard.enter_context(early_worker(target, args.source, args.expected_commit, args.detached_worker_id, args.resume))
            progress('verifying')
        stage = 'verify_git_source'
        anchor = Anchor(args.source, args.expected_commit)
        lifecycle = Lifecycle(anchor, target)
        if args.resume_detached:
            from install_detached import launch
            result, code = launch(lifecycle, args.resume_detached)
            print(json.dumps({'schema': 'personal-kb.workbuddy-install.v2', **result}, ensure_ascii=False, indent=2))
            return code
        action = ('resume' if args.resume else 'cancel_pending' if args.cancel_pending else
                  'rollback' if args.rollback else next(x for x in ('check', 'apply', 'update', 'status') if getattr(args, x)))
        stage = 'resume_or_install_transaction'
        result, code = run_bounded(lifecycle, action, args.wait_seconds,
            rollback_id=args.rollback, adopt_commit=args.adopt_commit,
            resume_id=args.resume, cancel_id=args.cancel_pending, progress=progress)
    except Refused as exc:
        result, code = {'status': 'refused', 'reason': exc.reason, 'impact': '安装未获准；保留代码和资料。若有中断事务，下次从可信克隆运行将尝试恢复。', 'next_step': exc.advice, 'target': target}, 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result, code = {'status': 'failed', 'reason': type(exc).__name__, 'error': {'operation': getattr(exc, 'install_operation', stage), 'path': getattr(exc, 'install_path', None) or getattr(exc, 'filename', None), 'destination': getattr(exc, 'filename2', None), 'errno': getattr(exc, 'errno', None), 'winerror': getattr(exc, 'winerror', None)}, 'impact': '操作未完成；不代表用户资料损坏。', 'next_step': '保留管理目录，从可信克隆重试以恢复事务；请宿主Agent检查路径权限或管理记录。', 'target': target}, 2
    finally:
        guard.close()
    if progress:
        try:
            progress('activated' if code == 0 else 'waiting' if code == 3 else 'failed', complete=code == 0, worker_finished=True, result_status=result.get('status'), reason=result.get('reason'), error=result.get('error'))
        except OSError as exc:
            result['progress_warning'] = {'reason': type(exc).__name__, 'path': getattr(exc, 'filename', None)}
    print(json.dumps({'schema': 'personal-kb.workbuddy-install.v2', **result}, ensure_ascii=False, indent=2))
    return code
