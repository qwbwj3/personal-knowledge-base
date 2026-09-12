"""Derived Obsidian view. Caller supplies one validated, authorized effective view.

No GUI, extraction, source scan, registration, or authoritative KB writes.
Callers must serialize sync calls for a vault (inspect is always read-only).
"""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from urllib.parse import quote
from typing import NamedTuple
from extraction_cache import normalize as normalize_extraction_cache

SCHEMA = "personal-kb.obsidian-view.v1"
MANIFEST = ".pkb-view.json"
HOME = "自动视图/首页.md"
SECTIONS = ("产品", "业务", "创作", "规则", "待确认", "历史与停用", "主题关联图")
PRESERVED = ".pkb-preserved"


class ConcurrentEdit(ValueError):
    pass


def location(kb_root: Path, config: dict) -> dict:
    vault = Path(kb_root).absolute() / "Obsidian知识库"
    home = vault / HOME
    return {"vault_path": str(vault), "home_note": str(home),
            "open_uri": "obsidian://open?path=" + quote(str(home), safe=""),
            "first_open_instructions": "首次请在 Obsidian 选择 Open folder as vault（打开文件夹作为仓库），选择 vault_path。注册后才可使用 open_uri；URI 不会自动创建或注册 Vault。"}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _path(root: Path, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute() or not p.parts or any(x in ("..", ".") for x in p.parts):
        raise ValueError("unsafe_path:" + rel)
    target = root / p
    for ancestor in (root, target, *(root / Path(*p.parts[:i]) for i in range(1, len(p.parts)))):
        if ancestor.is_symlink():
            raise ValueError("symlink:" + str(ancestor))
    return target


# Bound binary payload memory independently of total original/attachment bytes.
# Extracted text and rendered notes still occupy memory; this is not an O(1)
# claim for the complete knowledge-base pipeline.
COPY_CHUNK_BYTES = 1024 * 1024


class _Attachment(NamedTuple):
    root: Path
    relative: str
    sha256: str


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(COPY_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _payload_hash(data: bytes | _Attachment) -> str:
    return data.sha256 if isinstance(data, _Attachment) else _hash(data)


def _write_payload(stream, data: bytes | _Attachment) -> None:
    if not isinstance(data, _Attachment):
        stream.write(data)
        return
    # Re-check the lexical path at the time of use and hash the bytes actually
    # copied. A preflight digest alone is not evidence for a later copy.
    source = _path(data.root, data.relative)
    if not source.is_file():
        raise ValueError("attachment_source_not_file:" + data.relative)
    digest = hashlib.sha256()
    with source.open("rb") as original:
        for block in iter(lambda: original.read(COPY_CHUNK_BYTES), b""):
            digest.update(block)
            stream.write(block)
    if digest.hexdigest() != data.sha256:
        raise ValueError("attachment_copy_identity_mismatch:" + data.relative)


def _read_manifest(vault: Path) -> dict:
    path = _path(vault, MANIFEST)
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("state") not in ("ready", "in_progress", "invalidated"):
        raise ValueError("invalid_manifest")
    for field in ("files", "base_files"):
        if not isinstance(doc.get(field, {}), dict):
            raise ValueError("invalid_manifest_files")
        for rel, digest in doc.get(field, {}).items():
            if not rel.startswith(("自动视图/", "附件/")) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid_managed_file")
            _path(vault, rel)
    return doc


def _atomic(path: Path, data: bytes | _Attachment) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp files are intentionally not unlinked, even on interrupted writes.
    fd, temporary = tempfile.mkstemp(prefix=".pkb-write-", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "wb") as stream:
        _write_payload(stream, data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if hasattr(os, "O_DIRECTORY"):
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _preserved_conflicts(vault: Path) -> list[dict]:
    """Captured inodes retain even writes from editors with an old open handle."""
    conflicts = []
    directory = _path(vault, PRESERVED)
    if not directory.exists():
        return conflicts
    for item in directory.iterdir():
        record = _path(vault, item.relative_to(vault).as_posix() + "/record.json")
        if not record.is_file():
            continue  # An interrupted new empty capture contains no user data.
        value = json.loads(record.read_text(encoding="utf-8"))
        prior = _path(vault, item.relative_to(vault).as_posix() + "/prior")
        if prior.is_file() and _file_hash(prior) not in value["expected"]:
            conflicts.append({"file": value["file"], "preserved_path": str(prior),
                              "message": "并发人工修改已保留；请先将保留稿移入个人笔记，再决定恢复自动页。"})
    return conflicts


def _publish(vault: Path, rel: str, data: bytes | _Attachment, expected: set) -> None:
    """Capture, validate, then publish without ever replacing an existing inode.

    A second pre-write hash check cannot close the editor race. Moving the old
    inode to a durable, uniquely named capture preserves even late open-handle
    writes. link() atomically fails if an editor recreates the target. Captures
    are intentionally retained; unsupported filesystems fail, never fall back
    to destructive replace/unlink. The manifest journal precedes this protocol.
    """
    path = _path(vault, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    capture = _path(vault, PRESERVED + "/" + uuid.uuid4().hex)
    capture.mkdir(parents=True)
    _atomic(capture / "record.json", _json({"file": rel, "expected": sorted(expected)}))
    _atomic(capture / "new", data)
    prior = capture / "prior"
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise ConcurrentEdit("concurrent_non_file:" + rel)
        os.rename(path, prior)
        if hasattr(os, "O_DIRECTORY"):
            for directory in (capture, path.parent):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        if _file_hash(prior) not in expected:
            try:
                os.link(prior, path)  # Restore only if the editor has not saved a newer file.
            except FileExistsError:
                pass
            raise ConcurrentEdit("concurrent_user_changes_preserved:" + rel)
    try:
        os.link(capture / "new", path)
    except FileExistsError as exc:
        raise ConcurrentEdit("concurrent_target_recreated:" + rel) from exc
    if hasattr(os, "O_DIRECTORY"):
        for directory in (capture, path.parent):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def _safe(value: object) -> str:
    s = re.sub(r'[\x00-\x1f\\/:*?"<>|#\[\]^`]', "＿", str(value))
    return s.strip(" .")[:70] or "未命名"


def _literal(value: object) -> str:
    # All supplied metadata and text are data, never live Markdown/HTML links.
    return html.escape(str(value)).replace("[", "&#91;").replace("]", "&#93;")


def _link(rel: str, label: str = "") -> str:
    return "[[" + rel + ("|" + _safe(label) if label else "") + "]]"


def _identity(item: dict) -> str:
    return _hash(_json({k: item.get(k) for k in
                       ("document_id", "revision_id", "extraction_id", "sha256", "source_relative")}))


def _authorized(config: dict, view: dict) -> bool:
    return (view.get("control", {}).get("authorization", config.get("authorization", {}))).get("read") is True


def _build(view: dict) -> dict[str, bytes | _Attachment]:
    release = Path(view["release"])
    cache = normalize_extraction_cache(json.loads(_path(release, "extractions.json").read_text(encoding="utf-8")), view["catalog"].get("snapshots", []))
    current = {_identity(x) for x in view["effective_current"]}
    leads = {_identity(x) for x in view["catalog"].get("leads", [])}
    stopped = {(x.get("document_id"), x.get("file")) for x in view.get("restricted", [])}
    items = {}
    for x in [*view["catalog"].get("snapshots", []), *view["catalog"].get("leads", []),
              *view.get("raw_current", []), *view["effective_current"]]:
        items[_identity(x)] = x
    files, groups, topics = {}, {x: [] for x in SECTIONS}, {}
    stamp = _literal(json.dumps(view["receipt"], ensure_ascii=False, sort_keys=True))
    banner = "> 自动派生视图；非权威库。修改请写入个人笔记，不要编辑自动页。\n\n"
    for key, item in sorted(items.items()):
        title = str(item.get("业务对象") or item.get("source_relative") or "资料")
        rel = "自动视图/资料/" + _safe(Path(str(item.get("source_relative") or title)).stem) + "-" + key[:20] + ".md"
        blocked = (item.get("document_id"), item.get("source_relative")) in stopped
        status = "当前依据" if key in current and not blocked else ("待确认线索（未核验，不可作当前依据）" if key in leads and not blocked else "历史／停用（不可作当前依据）")
        kind = str(item.get("资料类型", ""))
        section = {"产品资料": "产品", "个人业务资料": "业务", "个人内容资料": "创作", "当前规则与决定": "规则"}.get(kind, "待确认")
        group = section if status == "当前依据" else ("待确认" if key in leads and not blocked else "历史与停用")
        groups[group].append(_link(rel, title))
        body = ["# " + _safe(title), banner, "**" + status + "**", "来源：<span>" + _literal(item.get("source_relative", "")) + "</span>",
                "原状态（仅存档）：" + _literal(item.get("状态", "")),
                "版本：" + _literal(item.get("document_version") or item.get("版本", "")),
                "适用版本：" + _literal(item.get("applicable_version", "")),
                "revision_id：" + _literal(item.get("revision_id", "")),
                "extraction_id：" + _literal(item.get("extraction_id", "")), "视图回执：" + stamp,
                _link(HOME, "返回首页")]
        if blocked:
            body.append("最新停用控制已生效；旧发布中的当前状态不得继续使用。")
        authority = ("人工确认元数据导航（不表示语义事实）" if item.get("metadata_authority") == "person_confirmed"
                     else "自动分类导航（非人工确认／语义事实）")
        labels = ["类型：" + kind] if kind else []
        product_name = item.get("产品名称") or item.get("product_name")
        product_id = item.get("产品身份") or item.get("product_id")
        if product_name:
            labels.append("产品：" + str(product_name))
        if product_id:
            labels.append("产品身份：" + str(product_id))
        tags = item.get("标签") or item.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]  # no guessed tokenization/semantic inference
        labels += ["标签：" + str(t) for t in tags]
        if labels:
            body.append(authority)
        for label in dict.fromkeys(labels):
            topic = "自动视图/主题/" + _safe(label) + "-" + _hash(label.encode())[:12] + ".md"
            topics.setdefault(topic, (label, []))[1].append(_link(rel, title) + " — " + status + "；" + authority)
            body.append(_link(topic, label))
        extraction_id = str(item.get("extraction_id") or "")
        extracted = cache.get(extraction_id)
        if not extracted:
            body.append("## 全文不可用\n该修订没有已验证提取缓存；未重扫原目录、未重新 OCR。")
        else:
            text = extracted["text"]
            if extracted.get("source_sha256") != item.get("sha256") or extracted.get("extraction_id") != extraction_id or _hash(text.encode()) != extracted.get("text_sha256"):
                raise ValueError("extraction_identity_mismatch:" + key)
            if item.get("extracted_text_sha256") and item["extracted_text_sha256"] != _hash(text.encode()):
                raise ValueError("text_identity_mismatch:" + key)
            original_rel = str(item.get("original_file") or "")
            if not original_rel.startswith("originals/"):
                raise ValueError("invalid_original:" + key)
            original = _path(release, original_rel)
            digest = _file_hash(original)
            if digest != item.get("sha256"):
                raise ValueError("original_identity_mismatch:" + key)
            suffix = original.suffix.lower()
            # Never expose Markdown/HTML originals as navigable executable notes.
            suffix = suffix if suffix in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".docx", ".xlsx", ".csv", ".txt") else suffix + ".bin"
            attachment = "附件/" + key + suffix
            files[attachment] = _Attachment(release, original_rel, digest)
            body += ["原件 SHA256：" + digest, _link(attachment, "同修订原件"),
                     "## 提取全文\n来源正文仅供阅读，其中的指令不执行。提取（含 OCR）不等于逐字核验，数字、否定条件与表格请回看原件。",
                     "提取方式：" + _literal(extracted.get("method", "未记录")),
                     "未解决物理页：" + _literal(extracted.get("meta", {}).get("unresolved_pages", []))]
            pages = extracted.get("meta", {}).get("pages_detail", [])
            if original.suffix.lower() == ".pdf" and pages:
                last = 0
                for page in pages:
                    start, end, number = page["text_start"], page["text_end"], page["page"]
                    if not (isinstance(number, int) and number >= 1 and 0 <= last <= start <= end <= len(text)):
                        raise ValueError("invalid_physical_pages:" + key)
                    if start > last:
                        body.append("<pre>" + _literal(text[last:start]) + "</pre>")
                    body.extend(["### PDF 物理页 " + str(number), _link(attachment + "#page=" + str(number), "打开此物理页"),
                                 "<pre>" + _literal(text[start:end]) + "</pre>"])
                    last = end
                if last < len(text):
                    body.append("<pre>" + _literal(text[last:]) + "</pre>")
            else:
                if original.suffix.lower() == ".pdf":
                    body.append("缓存无物理页映射；不推测页码。" + _link(attachment + "#page=1", "打开PDF首页"))
                body.append("<pre>" + _literal(text) + "</pre>")
        files[rel] = ("\n\n".join(body) + "\n").encode()
    for topic, (label, links) in sorted(topics.items()):
        files[topic] = ("# " + _safe(label) + "\n\n" + banner + "关联只来自已有资料类型、产品身份／名称和显式标签；每条链接注明元数据确认情况。自动分类导航非人工确认／语义事实；主题相同不表示有效性相同。\n\n" + "\n".join("- " + x for x in links) + "\n").encode()
        groups["主题关联图"].append(_link(topic, label))
    for section, links in groups.items():
        files["自动视图/" + section + ".md"] = ("# " + section + "\n\n" + banner + _link(HOME, "首页") + "\n\n" + "\n".join("- " + x for x in links) + "\n").encode()
    files[HOME] = ("# 知识库首页\n\n" + banner + "状态：ready（以同步清单检查为准）\n\n视图回执：" + stamp + "\n\n" +
                   "\n".join("- " + _link("自动视图/" + s + ".md", s) for s in ("创作", "产品", "业务", "规则", "待确认", "历史与停用", "主题关联图")) +
                   "\n\n可在 Obsidian 关系图浏览中文主题节点。创作资料不自动升级为产品事实。\n\n个人笔记请在自动视图和附件之外自行新建；本模块不写个人笔记和 .obsidian。\n").encode()
    return files


def _preflight(vault: Path, manifest: dict, desired: dict) -> tuple[list, list]:
    conflicts, missing = [], []
    old = manifest.get("files", {})
    base = manifest.get("base_files", {}) if manifest.get("state") == "in_progress" else {}
    for rel in sorted(set(old) | set(base) | set(desired)):
        path = _path(vault, rel)
        if not path.exists():
            if rel in old or rel in base:
                missing.append(rel)
            continue
        if not path.is_file():
            conflicts.append(rel)
            continue
        digest = _file_hash(path)
        allowed = {old.get(rel), base.get(rel)} - {None}
        if rel == HOME and manifest.get("state") == "in_progress":
            allowed.add(manifest.get("intermediate_home_sha256"))
        if not allowed or digest not in allowed:
            conflicts.append(rel)
    return conflicts, missing


def inspect(kb_root, config, view: dict) -> dict:
    """Read-only diagnostics; never repairs or creates files/directories."""
    result = {**location(kb_root, config), "schema": SCHEMA, "read_only": True,
              "receipt": view["receipt"], "writes": 0, "conflicts": [], "missing": []}
    if not _authorized(config, view):
        return {**result, "status": "needs_attention", "issues": ["read_not_authorized"]}
    try:
        vault = Path(result["vault_path"])
        manifest = _read_manifest(vault)
        desired = _build(view)
        conflicts, missing = _preflight(vault, manifest, desired)
        preserved = _preserved_conflicts(vault)
        missing = sorted(set(missing) | {r for r in desired if not _path(vault, r).exists()})
        stale = manifest.get("receipt") != view["receipt"]
        incomplete = manifest.get("state") == "in_progress"
        drift = sorted(r for r, b in desired.items() if manifest.get("files", {}).get(r) != _payload_hash(b))
        issues = (["not_synced"] if not manifest else []) + (["stale_receipt"] if stale else []) + (["in_progress"] if incomplete else []) + (["invalidated"] if manifest.get("state") == "invalidated" else [])
        return {**result, "status": "needs_attention" if issues or conflicts or missing or drift or preserved else "ready",
                "issues": issues, "conflicts": conflicts, "missing": missing, "outdated_files": drift,
                "preserved_conflicts": preserved,
                "stored_receipt": manifest.get("receipt"), "in_progress": incomplete, "managed_files": len(manifest.get("files", {}))}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, "status": "needs_attention", "issues": [str(exc)]}


def sync(kb_root, config, view: dict) -> dict:
    """Preflight all user edits before writing; resume interrupted managed writes."""
    result = {**location(kb_root, config), "schema": SCHEMA, "receipt": view["receipt"],
              "writes": 0, "conflicts": [], "missing": []}
    if not _authorized(config, view):
        return {**result, "status": "needs_attention", "issues": ["read_not_authorized"]}
    try:
        vault = Path(result["vault_path"])
        manifest = _read_manifest(vault)
        desired = _build(view)
        # Retire obsolete notes rather than deleting; retained attachments are never current navigation.
        old = {**manifest.get("base_files", {}), **manifest.get("files", {})}
        for rel in old.keys() - desired.keys():
            path = _path(vault, rel)
            if rel.endswith(".md"):
                desired[rel] = ("# 已失效的自动页\n\n此页已不属于有效知识库视图，不可作当前依据。\n\n" + _link(HOME, "请查看最新首页") + "\n").encode()
            elif path.is_file():
                desired[rel] = _Attachment(vault, rel, _file_hash(path))
        conflicts, missing = _preflight(vault, manifest, desired)
        result.update(conflicts=conflicts, missing=missing)
        preserved = _preserved_conflicts(vault)
        result["preserved_conflicts"] = preserved
        if conflicts or preserved:
            return {**result, "status": "needs_attention", "issues": ["user_changes_preserved"]}
        target = {rel: _payload_hash(data) for rel, data in desired.items()}
        ready = {"schema": SCHEMA, "state": "ready", "receipt": view["receipt"], "files": target}
        if manifest == ready and not missing:
            return {**result, "status": "ready", "managed_files": len(target)}
        # Journal precedes every content mutation; include temporary home in allowed recovery hashes.
        updating = "# 知识库视图同步未完成\n\n请让Agent检查／修复我的知识库；完成前不要将旧页面作为当前依据。\n".encode()
        journal = {**ready, "state": "in_progress", "base_files": old}
        # The home transition must be a journaled intermediate hash, not an untracked edit.
        journal["intermediate_home_sha256"] = _hash(updating)
        _atomic(_path(vault, MANIFEST), _json(journal)); result["writes"] += 1
        home_allowed = {manifest.get("files", {}).get(HOME), manifest.get("base_files", {}).get(HOME),
                        manifest.get("intermediate_home_sha256")} - {None}
        _publish(vault, HOME, updating, home_allowed); result["writes"] += 1
        for rel, data in sorted(desired.items(), key=lambda pair: pair[0] == HOME):
            path = _path(vault, rel)
            if path.is_file() and _file_hash(path) == target[rel]:
                continue
            expected = {manifest.get("files", {}).get(rel), manifest.get("base_files", {}).get(rel)} - {None}
            if rel == HOME:
                expected.add(_hash(updating))
            _publish(vault, rel, data, expected); result["writes"] += 1
        preserved = _preserved_conflicts(vault)
        if preserved or any(not _path(vault, rel).is_file() or _file_hash(_path(vault, rel)) != digest for rel, digest in target.items()):
            raise ConcurrentEdit("concurrent_edits_detected_at_completion")
        _atomic(_path(vault, MANIFEST), _json(ready)); result["writes"] += 1
        return {**result, "status": "ready", "managed_files": len(target), "recovered": manifest.get("state") == "in_progress"}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, "status": "needs_attention", "issues": [str(exc)], "in_progress": True,
                "preserved_conflicts": _preserved_conflicts(vault) if isinstance(exc, ConcurrentEdit) else result.get("preserved_conflicts", [])}


def inspect_invalidated(kb_root, config) -> dict:
    """Revoked control-only status: no release, source or extracted-text reads."""
    result = {**location(kb_root, config), "read_only": True, "writes": 0, "local_copies_remain": True}
    try:
        vault = Path(result["vault_path"])
        manifest = _read_manifest(vault)
        if not manifest:
            return {**result, "status": "ready", "invalidated": True, "issues": ["no_managed_view"]}
        home = _path(vault, HOME)
        complete = (manifest.get("state") == "invalidated" and home.is_file()
                    and _file_hash(home) == manifest.get("files", {}).get(HOME))
        return {**result, "status": "ready" if complete else "needs_attention", "invalidated": complete,
                "view_state": manifest.get("state")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, "status": "needs_attention", "invalidated": False, "issues": [str(exc)]}


def invalidate(kb_root, config, reason: str) -> dict:
    """Mark local copies revoked without reading any release/source/extraction.

    Revocation is not remote erasure and cannot block Obsidian visibility.
    """
    result = {**location(kb_root, config), "schema": SCHEMA, "writes": 0,
              "conflicts": [], "local_copies_remain": True}
    try:
        vault = Path(result["vault_path"])
        manifest = _read_manifest(vault)
        if not manifest:
            return {**result, "status": "ready", "invalidated": True, "issues": ["no_managed_view"]}
        home = _path(vault, HOME)
        allowed = {manifest.get("files", {}).get(HOME), manifest.get("base_files", {}).get(HOME),
                   manifest.get("intermediate_home_sha256")} - {None}
        if home.exists() and (not home.is_file() or _file_hash(home) not in allowed):
            return {**result, "status": "needs_attention", "conflicts": [HOME], "issues": ["user_changes_preserved"]}
        message = ("# Agent 读取授权已撤销\n\n自动视图不再更新；旧页不可视为最新依据。\n\n"
                   "旧本地页面、附件和个人笔记仍然存在，Obsidian 仍可查看。撤销 Agent 读取权不等于远程擦除，也不会阻断本地可见性。\n\n"
                   "原因：<pre>" + _literal(reason) + "</pre>\n").encode()
        files = {**manifest.get("base_files", {}), **manifest.get("files", {}), HOME: _hash(message)}
        ready = {"schema": SCHEMA, "state": "invalidated", "receipt": manifest.get("receipt"), "files": files,
                 "reason": reason, "local_copies_remain": True}
        if not (manifest == ready and home.is_file() and _file_hash(home) == _hash(message)):
            journal = {**ready, "state": "in_progress", "base_files": manifest.get("files", {}),
                       "intermediate_home_sha256": manifest.get("intermediate_home_sha256")}
            _atomic(_path(vault, MANIFEST), _json(journal)); result["writes"] += 1
            _publish(vault, HOME, message, allowed); result["writes"] += 1
            _atomic(_path(vault, MANIFEST), _json(ready)); result["writes"] += 1
        return {**result, "status": "ready", "invalidated": True}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, "status": "needs_attention", "issues": [str(exc)]}
