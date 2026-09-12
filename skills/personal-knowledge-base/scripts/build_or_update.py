#!/usr/bin/env python3
"""Run the workspace guard, then the proven llm-wiki-local ingest pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from workspace_guard import inspect_workspace


CATALOG_RELATIVE = Path(".personal-kb/catalog.json")


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def normalized(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", value).casefold()
        if c.isalnum() or "\u3400" <= c <= "\u9fff"
    )


def aliases(row: dict[str, str]) -> list[str]:
    values = [row.get("业务对象", ""), row.get("产品简称", "")]
    result: set[str] = set()
    for value in values:
        for part in re.split(r"[,，、;；|/]", value):
            part = re.sub(r"[（(].*?[)）]", "", part).strip()
            if part:
                result.add(part)
                without_year = re.sub(r"(?:19|20)\d{2}", "", part).strip(" -_（）()")
                if len(normalized(without_year)) >= 3:
                    result.add(without_year)
    # Filename-derived aliases never replace product identity; they only help
    # resolve ordinary questions after the catalog has already bound the file.
    stem = Path(row.get("资料路径", "")).stem
    stem = re.split(r"(?:正式保险条款|保险条款|条款|产品说明书|说明书|培训问答|问答|利益演示)", stem)[0]
    stem = re.sub(r"[_-]+$", "", stem).strip()
    if len(normalized(stem)) >= 3:
        result.add(stem)
        result.add(re.sub(r"(?:19|20)\d{2}", "", stem).strip(" -_"))
    return sorted(x for x in result if x)


def snapshot_entry(root: Path, row: dict[str, str]) -> dict:
    path = root / row["资料路径"]
    digest = file_hash(path)
    subject = row.get("产品身份") or row["业务对象"]
    year = row.get("产品年份") or ""
    if not year:
        match = re.search(r"(?:19|20)\d{2}", row["业务对象"] + " " + row["资料路径"])
        year = match.group(0) if match else ""
    return {
        "snapshot_id": digest,
        "sha256": digest,
        "资料编号": row["资料编号"],
        "资料路径": row["资料路径"],
        "资料类型": row["资料类型"],
        "业务对象": row["业务对象"],
        "document_id": row.get("文档身份") or row["资料编号"],
        "product_id": normalized(subject),
        "product_name": subject,
        "product_aliases": aliases(row) if row["资料类型"] == "产品资料" else [],
        "product_year": year,
        "applicable_version": row.get("适用版本") or row["版本"],
        "document_version": row.get("文档版本") or row["版本"],
        "版本": row["版本"],
        "状态": row["状态"],
        "资料日期": row["资料日期"],
        "证据级别": row["证据级别"],
        "source_path": row.get("源文件路径") or row["资料路径"],
        "qualification": "lead" if row["状态"] == "待核验" else "evidence",
    }


def build_catalog(root: Path, vault: Path, check: dict) -> dict:
    catalog_path = vault / CATALOG_RELATIVE
    old: dict = {}
    if catalog_path.is_file():
        try:
            old = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
    history = {
        (str(item.get("snapshot_id")), str(item.get("document_id"))): item
        for item in old.get("snapshots", []) if isinstance(item, dict)
    }
    current: list[dict] = []
    for row in check["registry_rows"]:
        path = root / row["资料路径"]
        if row["资料路径"] == "04_当前规则与决定/资料登记表.csv" or not path.is_file():
            continue
        item = snapshot_entry(root, row)
        history[(item["snapshot_id"], item["document_id"])] = item
        if row["状态"] not in {"历史", "废弃"}:
            current.append(item)
    active_by_document = {str(item["document_id"]): str(item["snapshot_id"]) for item in current}
    for key, item in list(history.items()):
        active_hash = active_by_document.get(str(item.get("document_id")))
        if active_hash and str(item.get("snapshot_id")) != active_hash and item.get("状态") not in {"废弃"}:
            item = dict(item)
            item["状态"] = "历史"
            history[key] = item
    return {
        "schema": "personal-kb.activation-catalog.v2",
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(root),
        "current": current,
        "snapshots": list(history.values()),
        "pending_unregistered": check.get("unregistered_paths", []),
    }


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


def load_json_output(value: str) -> dict:
    start = value.find("{")
    if start < 0:
        raise ValueError("底层建库程序没有返回JSON")
    return json.loads(value[start:])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    scope_path = Path(args.scope).expanduser().resolve(strict=True)
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    roots = scope.get("allowed_source_roots") or []
    if len(roots) != 1:
        raise SystemExit("scope必须且只能包含一个资料目录")
    check = inspect_workspace(Path(roots[0]))
    if not check["passed"]:
        print(json.dumps({
            "schema": "personal-kb.build-result.v1",
            "applied": False,
            "workspace_check": check,
            "next_action": "修正明确错误；待整理新资料不会阻断已有知识库。",
        }, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    root = Path(roots[0]).expanduser().resolve(strict=True)
    vaults = scope.get("allowed_vaults") or []
    if len(vaults) != 1:
        raise SystemExit("scope必须且只能包含一个Vault")
    vault = Path(vaults[0]).expanduser().resolve(strict=False)
    if not args.apply and not vault.exists():
        print(json.dumps({
            "schema": "personal-kb.build-result.v2",
            "applied": False,
            "preview": True,
            "workspace_check": {
                "passed": True,
                "material_file_count": check["material_file_count"],
                "registered_count": check["registered_count"],
                "warnings": check["warnings"],
                "conflicts": check["conflicts"],
                "pending_unregistered": check.get("unregistered_paths", []),
            },
            "next_action": "预检通过；用户确认授权后应用，不需要预先创建Vault。",
        }, ensure_ascii=False, indent=2))
        return

    base = Path(__file__).with_name("start_vault.py")
    command = [
        sys.executable,
        str(base),
        "--scope", str(scope_path),
        "--operation-id", "personal-kb-" + uuid.uuid4().hex,
    ]
    if args.apply:
        command.append("--apply")
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        print(completed.stdout, end="")
        print(completed.stderr, file=sys.stderr, end="")
        raise SystemExit(completed.returncode)
    try:
        result = load_json_output(completed.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": "personal-kb.build-error.v1", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
    if args.apply and result.get("applied") is True:
        # Business labels are committed only after the content transaction has
        # succeeded.  Retrieval therefore never applies a newer register row
        # to an older stored snapshot.
        atomic_json(vault / CATALOG_RELATIVE, build_catalog(root, vault, check))
    print(json.dumps({
        "schema": "personal-kb.build-result.v2",
        "applied": result.get("applied") is True,
        "workspace_check": {
            "passed": True,
            "material_file_count": check["material_file_count"],
            "registered_count": check["registered_count"],
            "warnings": check["warnings"],
            "conflicts": check["conflicts"],
            "pending_unregistered": check.get("unregistered_paths", []),
        },
        "engine": result,
        "next_action": "知识库已更新；查询时按任务类型调用 retrieve_task.py。" if args.apply else "这是预览；用户确认后增加 --apply。",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
