#!/usr/bin/env python3
"""Validate the four-zone insurance-broker knowledge workspace.

The vendored llm-wiki-local runtime deliberately treats files as generic
sources.  This small policy layer supplies the business meaning required by
the workshop: source type, subject, version, status, and evidence level.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path, PurePosixPath


from pdf_pipeline import IMAGE_SUFFIXES

SUPPORTED = {".md", ".txt", ".csv", ".html", ".htm", ".docx", ".xlsx", ".pdf"} | IMAGE_SUFFIXES
REGISTRY = "04_当前规则与决定/资料登记表.csv"
ZONES = {
    "01_产品资料": "产品资料",
    "02_个人业务资料": "个人业务资料",
    "03_个人内容资料": "个人内容资料",
    "04_当前规则与决定": "当前规则与决定",
}
STATUSES = {"当前", "历史", "待核验", "废弃"}
REQUIRED_COLUMNS = (
    "资料编号",
    "资料路径",
    "资料类型",
    "业务对象",
    "版本",
    "状态",
    "资料日期",
    "证据级别",
    "说明",
)
OPTIONAL_COLUMNS = (
    "文档身份",
    "产品身份",
    "产品简称",
    "产品年份",
    "适用版本",
    "文档版本",
    "源文件路径",
)


def emit(value: dict, code: int = 0) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def safe_relative(value: str) -> str | None:
    value = value.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def zone_for(relative: str) -> str | None:
    first = PurePosixPath(relative).parts[0] if PurePosixPath(relative).parts else ""
    return ZONES.get(first)


def load_registry(root: Path) -> tuple[list[dict[str, str]], list[str]]:
    path = root / REGISTRY
    if not path.is_file():
        return [], [f"缺少资料登记表：{REGISTRY}"]
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            missing = [name for name in REQUIRED_COLUMNS if name not in columns]
            if missing:
                return [], ["资料登记表缺少列：" + "、".join(missing)]
            rows = []
            for number, raw in enumerate(reader, start=2):
                row = {name: (raw.get(name) or "").strip() for name in REQUIRED_COLUMNS}
                for name in OPTIONAL_COLUMNS:
                    row[name] = (raw.get(name) or "").strip()
                row["登记表行"] = str(number)
                rows.append(row)
    except (OSError, UnicodeError, csv.Error) as exc:
        return [], [f"资料登记表无法读取：{exc}"]
    return rows, errors


def inspect_workspace(root: Path) -> dict:
    root = root.expanduser().resolve(strict=True)
    errors: list[str] = []
    warnings: list[str] = []
    for folder in ZONES:
        if not (root / folder).is_dir():
            errors.append(f"缺少资料分区：{folder}")

    files: list[str] = []
    misplaced: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            errors.append(f"资料区不接受符号链接：{path.relative_to(root).as_posix()}")
            continue
        if not path.is_file() or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "README.md" or path.suffix.lower() not in SUPPORTED:
            continue
        files.append(relative)
        if zone_for(relative) is None:
            misplaced.append(relative)
    if misplaced:
        errors.extend(f"资料未放入四个分区：{value}" for value in misplaced)

    rows, registry_errors = load_registry(root)
    errors.extend(registry_errors)
    by_path: dict[str, dict[str, str]] = {}
    ids: set[str] = set()
    for row in rows:
        line = row["登记表行"]
        relative = safe_relative(row["资料路径"])
        if relative is None:
            errors.append(f"登记表第{line}行资料路径无效")
            continue
        row["资料路径"] = relative
        if relative in by_path:
            errors.append(f"登记表重复路径：{relative}")
        by_path[relative] = row
        if not row["资料编号"]:
            errors.append(f"登记表第{line}行缺少资料编号")
        elif row["资料编号"] in ids:
            errors.append(f"资料编号重复：{row['资料编号']}")
        ids.add(row["资料编号"])
        expected = zone_for(relative)
        if expected is None:
            errors.append(f"登记表路径不在四个分区：{relative}")
        elif row["资料类型"] != expected:
            errors.append(f"资料类型与文件夹不一致：{relative} 应为“{expected}”")
        if row["状态"] not in STATUSES:
            errors.append(f"登记表第{line}行状态无效：{row['状态'] or '空白'}")
        for field in ("业务对象", "版本", "资料日期", "证据级别"):
            if not row[field]:
                errors.append(f"登记表第{line}行缺少{field}")
        if not (root / relative).is_file():
            errors.append(f"登记表所列文件不存在：{relative}")

    material_files = [value for value in files if value != REGISTRY]
    unregistered = [value for value in material_files if value not in by_path]
    if unregistered:
        # A newly dropped file belongs to the maintenance queue.  It must not
        # make the last successfully activated knowledge base unavailable.
        warnings.append(f"发现{len(unregistered)}份待整理新资料；已有资料仍可查询。")

    # Different files can complement one another at one version.  Two current
    # version labels for the same business subject are an unresolved conflict.
    current_versions: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row["状态"] != "当前":
            continue
        key = (row["资料类型"], row.get("产品身份") or row["业务对象"])
        applicable = row.get("适用版本") or row["版本"]
        current_versions[key][applicable].append(row["资料路径"])
    conflicts = []
    for (kind, subject), versions in sorted(current_versions.items()):
        if len(versions) > 1:
            conflicts.append({
                "资料类型": kind,
                "业务对象": subject,
                "当前版本": [
                    {"版本": version, "资料路径": paths}
                    for version, paths in sorted(versions.items())
                ],
                "处理": "保留冲突并展示给用户；未确认前不得合并成单一事实。",
            })

    historical = [row["资料路径"] for row in rows if row["状态"] in {"历史", "废弃"}]
    unverified = [row["资料路径"] for row in rows if row["状态"] == "待核验"]
    if unverified:
        warnings.append(f"有{len(unverified)}份待核验资料，只能作为线索，不能单独确认产品事实。")
    if conflicts:
        warnings.append(f"发现{len(conflicts)}组当前版本冲突，检索时必须显式展示。")

    return {
        "schema": "personal-kb.workspace-check.v1",
        "workspace": str(root),
        "passed": not errors,
        "zones": ZONES,
        "supported_formats": sorted(SUPPORTED),
        "material_file_count": len(material_files),
        "registered_count": len(rows),
        "errors": errors,
        "warnings": warnings,
        "conflicts": conflicts,
        "historical_paths": historical,
        "unverified_paths": unverified,
        "unregistered_paths": unregistered,
        "registry_rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    try:
        result = inspect_workspace(Path(args.workspace))
    except (OSError, RuntimeError) as exc:
        emit({"schema": "personal-kb.workspace-check.v1", "passed": False, "errors": [str(exc)]}, 2)
    emit(result, 0 if result["passed"] else 2)


if __name__ == "__main__":
    main()
