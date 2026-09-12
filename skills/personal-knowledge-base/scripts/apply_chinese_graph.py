#!/usr/bin/env python3
"""Validate and apply a source-backed Chinese Obsidian graph plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

USER_START = "<!-- llm-wiki:user-notes:start -->"
USER_END = "<!-- llm-wiki:user-notes:end -->"
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
TAG = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff0-9/_-]+$")


def fail(message: str, code: int = 2, **details: object) -> None:
    payload = {"schema": "llm-wiki.chinese-graph-error.v1", "error": message}
    payload.update(details)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def resolved(value: str | Path, *, must_exist: bool = True) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        fail(f"cannot resolve path {str(value)!r}: {exc}")


def inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read JSON {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"JSON root must be an object: {path}")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def json_from_output(stdout: str, label: str) -> dict:
    start = stdout.find("{")
    if start < 0:
        fail(f"{label} returned no JSON")
    try:
        value = json.loads(stdout[start:])
    except ValueError as exc:
        fail(f"{label} returned invalid JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} returned a non-object JSON value")
    return value


def run_json(command: list[str], label: str, timeout: int = 300) -> dict:
    completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if completed.returncode != 0:
        fail(
            f"{label} failed with exit {completed.returncode}",
            stderr=completed.stderr.strip()[-4000:],
            stdout=completed.stdout.strip()[-4000:],
        )
    return json_from_output(completed.stdout, label)


def safe_title(value: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(value) <= 40 or not CJK.search(value):
        fail(f"graph title must be 1-40 characters and contain Chinese: {value!r}")
    if re.search(r'[<>:"/\\|?*\x00-\x1f#\[\]^]', value):
        fail(f"graph title contains an unsupported filename character: {value!r}")
    return value


def valid_tag(value: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(value) <= 48 or not CJK.search(value) or not TAG.fullmatch(value):
        fail(f"graph tag must be a compact Chinese tag without spaces or Latin letters: {value!r}")
    return value


def frontmatter(title: str, tags: list[str], date: str) -> str:
    return (
        "---\n"
        "type: concept\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        "status: developing\n"
        f"created: {date}\n"
        f"updated: {date}\n"
        "tags:\n"
        + "".join(f"  - {tag}\n" for tag in tags)
        + "---\n\n"
    )


def preserved_notes(existing: str | None) -> str:
    if not existing:
        return ""
    start = existing.find(USER_START)
    end = existing.find(USER_END)
    if start < 0 or end < start:
        return ""
    return existing[start + len(USER_START):end].strip("\n")


def user_section(notes: str) -> str:
    body = f"\n{notes}\n" if notes else "\n"
    return (
        "\n## 用户补充\n\n"
        "在下面两个标记之间写入的人工笔记，会在后续图谱更新时保留。\n\n"
        f"{USER_START}{body}{USER_END}\n"
    )


parser = argparse.ArgumentParser()
parser.add_argument("--scope", required=True)
parser.add_argument("--plan", required=True)
parser.add_argument("--bundle-out")
parser.add_argument("--operation-id")
parser.add_argument("--generated-at")
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()

scope_path = resolved(args.scope)
scope = load_json(scope_path)
if scope.get("schema") != "llm-wiki.privacy-scope.v2":
    fail("unsupported privacy scope schema")
if scope.get("mode") != "query-and-ingest":
    fail("scope mode does not permit graph writes")
allowed_vaults = [resolved(value, must_exist=True) for value in scope.get("allowed_vaults", [])]
forbidden = [resolved(value, must_exist=False) for value in scope.get("forbidden_roots", [])]
if len(allowed_vaults) != 1:
    fail("graph apply requires exactly one allowed vault")
vault = allowed_vaults[0]
if inside(vault, forbidden):
    fail("vault conflicts with forbidden_roots")

plan_path = resolved(args.plan)
plan = load_json(plan_path)
if plan.get("schema") != "llm-wiki.chinese-graph-plan.v1" or not isinstance(plan.get("nodes"), list):
    fail("unsupported Chinese graph plan schema")
if not 3 <= len(plan["nodes"]) <= 24:
    fail("Chinese graph plan must contain 3 to 24 nodes")

ledger_path = vault / "wiki/meta/ledgers/source-ledger.json"
ledger = load_json(ledger_path)
if ledger.get("schema") != "claude-obsidian.source-ledger.v1" or not isinstance(ledger.get("sources"), dict):
    fail("source ledger has an unsupported shape")
active_pages: set[str] = set()
for record in ledger["sources"].values():
    if not isinstance(record, dict) or record.get("review_status") != "active":
        continue
    for page in record.get("pages", []):
        if isinstance(page, str) and page.startswith("wiki/sources/") and not page.endswith("来源导航.md"):
            active_pages.add(page)

nodes: list[dict] = []
titles: set[str] = set()
for raw in plan["nodes"]:
    if not isinstance(raw, dict):
        fail("every graph node must be an object")
    title = safe_title(raw.get("title") if isinstance(raw.get("title"), str) else "")
    if title in titles:
        fail(f"duplicate graph title: {title}")
    titles.add(title)
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > 1000:
        fail(f"graph summary must contain 1 to 1000 characters: {title}")
    if "<!--" in summary or "---" in summary:
        fail(f"graph summary contains unsupported control markup: {title}")
    raw_tags = raw.get("tags")
    if not isinstance(raw_tags, list) or not 1 <= len(raw_tags) <= 6:
        fail(f"graph node must contain 1 to 6 tags: {title}")
    tags = []
    for value in raw_tags:
        if not isinstance(value, str):
            fail(f"graph tag must be a string: {title}")
        normalized = valid_tag(value)
        if normalized not in tags:
            tags.append(normalized)
    if "状态/自动生成" not in tags:
        tags.append("状态/自动生成")
    raw_sources = raw.get("source_pages")
    if not isinstance(raw_sources, list) or not raw_sources:
        fail(f"graph node must cite at least one source page: {title}")
    source_pages = []
    for source in raw_sources:
        if not isinstance(source, str) or source not in active_pages:
            fail(f"graph node references a non-current source page: {title}: {source!r}")
        source_path = (vault / source).resolve(strict=True)
        if not inside(source_path, [(vault / "wiki/sources").resolve()]) or inside(source_path, forbidden):
            fail(f"graph source page escaped the authorized source root: {source}")
        if source not in source_pages:
            source_pages.append(source)
    related = raw.get("related", [])
    if not isinstance(related, list) or any(not isinstance(value, str) for value in related):
        fail(f"graph related must be a string list: {title}")
    nodes.append({
        "title": title,
        "summary": summary.strip(),
        "tags": tags,
        "source_pages": source_pages,
        "related": related,
    })

for node in nodes:
    normalized_related = []
    for value in node["related"]:
        related = safe_title(value)
        if related == node["title"]:
            continue
        if related not in titles:
            fail(f"graph relation points to an unknown node: {node['title']} -> {related}")
        if related not in normalized_related:
            normalized_related.append(related)
    node["related"] = normalized_related

skill_root = Path(__file__).resolve().parents[1]
product_root = skill_root / "vendor/claude-obsidian"
core = product_root / "scripts/claude-obsidian.py"
prefix = product_root / "scripts/contextual-prefix.py"
bm25 = product_root / "scripts/bm25-index.py"
for required in (core, prefix, bm25):
    if not required.is_file():
        fail(f"vendored claude-obsidian runtime is incomplete: {required.name}")

now = args.generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
date = now[:10]
seed = hashlib.sha256((str(vault) + "\0" + now + "\0graph").encode()).hexdigest()[:10]
operation_id = args.operation_id or f"llm-wiki-graph-{now.replace(':','').replace('-','')[:15]}-{seed}"

writes: list[dict] = []
expected: dict[str, str | None] = {}
address_requests: list[dict] = []


def add_write(relative: str, content: str, *, address: bool = False) -> None:
    target = vault / relative
    current = file_hash(target) if target.is_file() else None
    expected[relative] = current
    writes.append({"path": relative, "mode": "replace" if current else "create", "content": content})
    if address:
        address_requests.append({"path": relative, "prefix": "c"})


node_paths: dict[str, str] = {}
for node in nodes:
    relative = f"wiki/知识图谱/{node['title']}.md"
    node_paths[node["title"]] = relative
    existing_path = vault / relative
    existing = existing_path.read_text(encoding="utf-8") if existing_path.is_file() else None
    content = frontmatter(node["title"], node["tags"], date)
    content += f"# {node['title']}\n\n"
    content += "> [!note] 图谱用途\n> 本页用于主题导航。事实回答必须回到下方来源页。\n\n"
    content += "## 导航摘要\n\n" + node["summary"] + "\n"
    if node["related"]:
        content += "\n## 关联主题\n\n" + "".join(f"- [[{value}]]\n" for value in node["related"])
    content += "\n## 来源证据\n\n"
    for source in node["source_pages"]:
        content += f"- [[{PurePosixPath(source).stem}]] — `{source}`\n"
    content += user_section(preserved_notes(existing))
    add_write(relative, content, address=True)

entry_path = "wiki/知识图谱/知识图谱入口.md"
entry_existing_path = vault / entry_path
entry_existing = entry_existing_path.read_text(encoding="utf-8") if entry_existing_path.is_file() else None
entry = frontmatter("知识图谱入口", ["知识图谱/入口", "状态/自动生成"], date)
entry += "# 知识图谱入口\n\n本页汇总当前中文主题节点。点击关系图可查看主题与 Tag 连接。\n\n## 主题节点\n\n"
for node in nodes:
    entry += f"- [[{node['title']}]] — {node['summary']}\n"
entry += user_section(preserved_notes(entry_existing))
add_write(entry_path, entry, address=True)

index_path = "wiki/index.md"
index_text = (vault / index_path).read_text(encoding="utf-8")
graph_block = "## 中文图谱\n\n- [[知识图谱入口]]\n\n"
if graph_block not in index_text:
    heading = "# 知识库入口\n\n"
    index_text = index_text.replace(heading, heading + graph_block, 1) if heading in index_text else index_text + "\n" + graph_block
add_write(index_path, index_text)

overview_path = "wiki/overview.md"
overview_text = (vault / overview_path).read_text(encoding="utf-8")
if "[[知识图谱入口]]" not in overview_text:
    overview_text = overview_text.rstrip() + "\n- [[知识图谱入口]]\n"
add_write(overview_path, overview_text)

manifest = {
    "schema": "llm-wiki.chinese-graph-manifest.v1",
    "generated_at": now,
    "operation_id": operation_id,
    "entry": entry_path,
    "nodes": [
        {
            "title": node["title"],
            "path": node_paths[node["title"]],
            "tags": node["tags"],
            "source_pages": node["source_pages"],
            "related": node["related"],
        }
        for node in nodes
    ],
}
add_write("wiki/meta/llm-wiki-graph.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

log_path = "wiki/log.md"
old_log = (vault / log_path).read_text(encoding="utf-8") if (vault / log_path).is_file() else ""
if f"## {date} — {operation_id}" not in old_log:
    marker = "# Wiki Log\n\n"
    entry_log = (
        f"## {date} — {operation_id}\n\n"
        f"- 应用 {len(nodes)} 个中文图谱节点。\n"
        f"- 可见 Tag 全部通过中文格式校验；节点引用 {len(set().union(*(set(n['source_pages']) for n in nodes)))} 个当前来源页。\n\n"
    )
    log = old_log.replace(marker, marker + entry_log, 1) if marker in old_log else old_log + "\n" + entry_log
    add_write(log_path, log)

bundle = {
    "schema": "claude-obsidian.transaction.v1",
    "operation_id": operation_id,
    "operation_type": "ingest",
    "expected_hashes": expected,
    "writes": writes,
    "address_requests": address_requests,
}
bundle_path = resolved(args.bundle_out, must_exist=False) if args.bundle_out else scope_path.with_name(f"{scope_path.stem}.{operation_id}.bundle.json")
atomic_json(bundle_path, bundle)
inspection = run_json(
    [sys.executable, str(core), "transaction", "inspect", str(bundle_path), "--vault", str(vault)],
    "Chinese graph transaction inspect",
)
approval = inspection.get("approval_sha256") or inspection.get("approved_plan_sha256")
if not isinstance(approval, str):
    fail("Chinese graph transaction inspection returned no approval hash")

result = None
prefix_result = None
bm25_result = None
lint_summary = None
if args.apply:
    result = run_json(
        [
            sys.executable,
            str(core),
            "transaction",
            "apply",
            str(bundle_path),
            "--vault",
            str(vault),
            "--approved-plan-sha256",
            approval,
        ],
        "Chinese graph transaction apply",
    )
    prefix_completed = subprocess.run(
        [sys.executable, str(prefix), "--vault", str(vault), "--all", "--no-llm"],
        text=True,
        capture_output=True,
        timeout=600,
    )
    if prefix_completed.returncode != 0:
        fail("contextual prefix failed after graph apply", stderr=prefix_completed.stderr.strip()[-4000:])
    prefix_result = prefix_completed.stdout.strip().splitlines()[-1] if prefix_completed.stdout.strip() else "complete"
    bm25_completed = subprocess.run(
        [sys.executable, str(bm25), "--vault", str(vault), "build"],
        text=True,
        capture_output=True,
        timeout=600,
    )
    if bm25_completed.returncode != 0:
        fail("BM25 build failed after graph apply", stderr=bm25_completed.stderr.strip()[-4000:])
    bm25_result = bm25_completed.stdout.strip().splitlines()[-1] if bm25_completed.stdout.strip() else "complete"
    lint_completed = subprocess.run(
        [sys.executable, str(core), "lint", "--vault", str(vault), "--format", "json", "--strict", "--as-of", date],
        text=True,
        capture_output=True,
        timeout=300,
    )
    lint_value = json_from_output(lint_completed.stdout, "strict lint")
    lint_summary = lint_value.get("summary")
    if lint_completed.returncode != 0:
        fail("strict lint found issues after Chinese graph apply", lint_summary=lint_summary)

print(json.dumps({
    "schema": "llm-wiki.chinese-graph-result.v1",
    "applied": bool(args.apply),
    "vault": str(vault),
    "operation_id": operation_id,
    "node_count": len(nodes),
    "entry_path": entry_path,
    "visible_titles_chinese": True,
    "visible_tags_chinese": True,
    "source_page_count": len(set().union(*(set(node["source_pages"]) for node in nodes))),
    "transaction": {
        "bundle": str(bundle_path),
        "approval_sha256": approval,
        "changed_paths": (result or {}).get("changed_paths", inspection.get("changed_paths", [])),
    },
    "retrieval": {"prefix": prefix_result, "bm25": bm25_result},
    "lint": lint_summary,
}, ensure_ascii=False, indent=2))
