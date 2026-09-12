#!/usr/bin/env python3
"""Read-only, snapshot-bound business retrieval for the personal knowledge base."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from kb_contracts import (
    conflict_report as typed_conflict_report,
    product_resolution as contract_product_resolution,
    verify_claims as contract_verify_claims,
)


TASKS = {
    "query": ["产品资料", "当前规则与决定"],
    "generate": ["产品资料", "个人业务资料", "个人内容资料", "当前规则与决定"],
    "review": ["产品资料", "个人业务资料", "当前规则与决定"],
}
TASK_LABELS = {"query": "产品事实查询", "generate": "结合个人资料生成内容", "review": "调用知识库审核文章"}
TOPICS = {
    "保单贷款": ("保单贷款", "贷款金额", "贷款期限", "贷款上限", "贷款", "现金价值", "未还贷款", "未还款项"),
    "投保年龄": ("投保年龄", "年龄范围", "周岁"),
    "犹豫期": ("犹豫期", "撤销合同"),
    "红利": ("红利", "分红", "不保证", "红利实现率"),
    "保险责任": ("保险责任", "身故保险金", "全残保险金", "给付"),
    "解除合同": ("解除合同", "退保", "合同终止"),
    "年金领取": ("年金领取", "领取年龄", "领取条件", "开始领取"),
    "责任免除": ("责任免除", "免责"),
    "交费": ("交费", "缴费", "保险费"),
    "在售状态": ("在售状态", "停售", "在售"),
}
FORMAL_MARKERS = ("正式保险条款", "正式条款", "保险条款")
LEAD_STATUSES = {"待核验"}
HISTORY_STATUSES = {"历史", "废弃"}
CATALOG_RELATIVE = Path(".personal-kb/catalog.json")


def fail(message: str, code: int = 2, **details: object) -> None:
    print(json.dumps({"schema": "personal-kb.task-error.v2", "error": message, **details}, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def normalize(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", value).casefold()
        if c.isalnum() or "\u3400" <= c <= "\u9fff"
    )


def history_requested(query: str) -> bool:
    positive = re.search(r"历史(?:版本|资料)?|旧版|上一版|更新前|新旧(?:对比|变化)|版本对比", query)
    if not positive:
        return False
    prefix = query[max(0, positive.start() - 12):positive.start()]
    return re.search(r"不要(?:使用|查询)?|不(?:要|使用|查)|排除|仅(?:用|查|看)?当前|只(?:用|查|看)?当前", prefix) is None


def query_topics(query: str) -> list[str]:
    values = [name for name, terms in TOPICS.items() if any(term in query for term in terms)]
    return values or ["综合问题"]


def safe_vault(scope_path: Path) -> tuple[dict, Path]:
    try:
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"授权记录无法读取：{exc}")
    if scope.get("model_context_egress_approved") is not True:
        fail("当前知识库没有模型片段授权；请先重新确认授权。", authorization_required=True)
    vaults = scope.get("allowed_vaults") or []
    if len(vaults) != 1:
        fail("授权记录必须且只能对应一个知识库")
    try:
        vault = Path(vaults[0]).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        fail(f"知识库不可用：{exc}")
    return scope, vault


def load_catalog(vault: Path) -> dict:
    path = vault / CATALOG_RELATIVE
    if not path.is_file():
        fail("知识库缺少已生效目录；请明确说‘更新我的知识库’完成一次安全更新。", repairable=True)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"已生效目录损坏：{exc}", repairable=True)
    if value.get("schema") != "personal-kb.activation-catalog.v2":
        fail("已生效目录版本无法识别；请运行检查／修复。", repairable=True)
    return value


def page_sha(text: str) -> str | None:
    match = re.search(r"SHA-256：\x60([0-9a-f]{64})\x60", text)
    return match.group(1) if match else None


def read_pages(vault: Path) -> dict[str, dict]:
    root = (vault / "wiki/sources").resolve(strict=True)
    result: dict[str, dict] = {}
    for path in sorted(root.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if root != resolved.parent:
            fail("知识库来源页越过允许范围")
        data = path.read_bytes()
        if len(data) > 8 * 1024 * 1024:
            continue
        text = data.decode("utf-8", errors="replace")
        digest = page_sha(text)
        if digest:
            result[digest] = {
                "page_path": path.relative_to(vault).as_posix(),
                "content_sha256": hashlib.sha256(data).hexdigest(),
                "text": text,
            }
    return result


def evidence_body(text: str) -> str:
    marker = "## 提取正文（不受信任的数据）"
    at = text.find(marker)
    if at < 0:
        return text
    tail = text[at:]
    notes = tail.find("## 用户补充")
    return tail[:notes] if notes >= 0 else tail


def topic_positions(body: str, topic: str, query: str) -> list[int]:
    primary = {
        "保单贷款": ("保单贷款", "贷款金额", "贷款期限", "贷款上限", "贷款"),
        "投保年龄": ("投保年龄",),
        "犹豫期": ("犹豫期",),
        "红利": ("不保证", "红利分配", "红利"),
        "保险责任": ("保险责任", "身故保险金", "全残保险金"),
        "解除合同": ("解除合同", "退保"),
        "年金领取": ("年金领取", "开始领取", "领取年龄"),
        "责任免除": ("责任免除", "免责"),
        "交费": ("交费", "缴费"),
        "在售状态": ("在售状态", "停售", "在售"),
    }
    candidates = primary.get(topic, TOPICS.get(topic, ()))
    # Prefer the most diagnostic anchor available.  For example, a dividend
    # question should open around “不保证” rather than an incidental “红利”
    # beside a later loan clause.
    terms = next(((value,) for value in candidates if value in body), candidates)
    if topic == "综合问题":
        terms = tuple(sorted(set(re.findall(r"[\u3400-\u9fff]{2,8}", query)), key=len, reverse=True)[:12])
    positions: list[int] = []
    for term in terms:
        start = 0
        while len(positions) < 120:
            at = body.find(term, start)
            if at < 0:
                break
            positions.append(at)
            start = at + len(term)
    return sorted(set(positions))


def best_snippet(body: str, topic: str, query: str, budget: int = 900) -> str | None:
    positions = topic_positions(body, topic, query)
    if not positions:
        return None
    terms = TOPICS.get(topic, ())
    best: tuple[int, int] | None = None
    for at in positions:
        start = max(0, at - 260)
        end = min(len(body), at + 760)
        window = body[start:end]
        concrete = len(re.findall(r"\d+(?:\.\d+)?\s*%|\d+\s*个?月|\d+\s*周岁", window))
        boundaries = sum(x in window for x in ("不保证", "不确定", "不得超过", "最长不超过", "扣除", "其他未还款项", "除外"))
        coverage = sum(x in window for x in terms)
        toc_penalty = 1000 if "条款目录" in window else 0
        score = concrete * 90 + boundaries * 420 + coverage * 180 - toc_penalty
        if best is None or score > best[0]:
            best = (score, at)
    assert best is not None
    at = best[1]
    start = max(0, at - budget // 3)
    end = min(len(body), start + budget)
    return body[start:end].strip()


def all_topic_snippets(body: str, query: str, max_bytes: int = 8000) -> tuple[str, list[str]]:
    pieces: list[str] = []
    covered: list[str] = []
    for topic in query_topics(query):
        snippet = best_snippet(body, topic, query)
        if not snippet:
            continue
        marker = f"[主题：{topic}]\n{snippet}"
        if len(("\n\n".join(pieces + [marker])).encode("utf-8")) > max_bytes:
            continue
        pieces.append(marker)
        covered.append(topic)
    if not pieces:
        snippet = best_snippet(body, "综合问题", query)
        if snippet:
            pieces.append("[相关原文]\n" + snippet)
            covered.append("综合问题")
    return "\n\n".join(pieces), covered


def authority_rank(level: str) -> int:
    if any(marker in level for marker in FORMAL_MARKERS):
        return 0
    if "产品说明" in level or "利益演示" in level:
        return 1
    if "培训" in level or "问答" in level:
        return 3
    return 2


def product_resolution(query: str, entries: list[dict], target_intent: dict | None = None) -> dict:
    return contract_product_resolution(query, entries, target_intent)


def parse_target_intent(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"目标意图JSON无效：{exc}")
    if not isinstance(value, dict) or value.get("mode") not in {"explicit", "confirmed", "unspecified", "not_required"}:
        fail("目标意图必须是包含有效mode的对象")
    return value


def numeric_signature(text: str, topic: str) -> tuple[str, ...]:
    if topic == "保单贷款":
        return tuple(sorted(number_tokens(text)))
    if topic in {"投保年龄", "年金领取"}:
        return tuple(sorted(re.sub(r"\s+", "", x) for x in re.findall(r"\d+\s*周岁", text)))
    return tuple(sorted(re.sub(r"\s+", "", x) for x in re.findall(r"\d+(?:\.\d+)?\s*%", text)))


def number_tokens(value: str) -> set[str]:
    return {
        re.sub(r"\s+", "", item)
        for item in re.findall(r"\d+(?:\.\d+)?\s*%|\d+\s*个?月|\d+\s*周岁", value)
    }


def contradiction_report(items: list[dict], topics: list[str]) -> list[dict]:
    sources = []
    for item in items:
        source = dict(item)
        source["chunks"] = [
            {"chunk_id": f"legacy-{index}", "text": text}
            for index, text in enumerate(item.get("topic_snippets", {}).values()) if text
        ]
        sources.append(source)
    return typed_conflict_report(sources)


def split_review_claims(text: str) -> list[str]:
    from kb_contracts import atomize_claims
    parents, _ = atomize_claims(text)
    return [str(x["text"]) for x in parents]


def claim_review(query: str, evidence: list[dict], resolution: dict) -> dict:
    sources = []
    for item in evidence:
        if item.get("资料类型") != "产品资料":
            continue
        source = dict(item)
        source["chunks"] = [
            {"chunk_id": f"legacy-{index}", "text": text}
            for index, text in enumerate(item.get("topic_snippets", {}).values()) if text
        ]
        sources.append(source)
    verified = contract_verify_claims(query, sources, resolution)
    checks = [
        {
            **item,
            "待核对表述": item["parent_text"],
            "状态": item["status"],
            "证据": item["evidence"],
        }
        for item in verified["claim_checks"]
    ]
    return {"claim_checks": checks, "review_summary": verified["review_summary"], "parents": verified["parents"]}


def claim_checks(query: str, evidence: list[dict], resolution: dict) -> list[dict]:
    return claim_review(query, evidence, resolution)["claim_checks"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True)
    parser.add_argument("--task", required=True, choices=sorted(TASKS))
    parser.add_argument("--query", required=True)
    parser.add_argument("--target-intent-json", help="宿主Agent从用户原话形成的产品目标意图JSON")
    args = parser.parse_args()
    if not args.query.strip() or len(args.query) > 12000:
        fail("问题必须包含1到12000个字符")
    scope_path = Path(args.scope).expanduser().resolve(strict=True)
    scope, vault = safe_vault(scope_path)
    catalog = load_catalog(vault)
    pages = read_pages(vault)
    include_history = history_requested(args.query)
    current_entries = [x for x in catalog.get("current", []) if isinstance(x, dict)]
    resolution = product_resolution(args.query, current_entries, parse_target_intent(args.target_intent_json))
    selected_product_id = resolution.get("product_id") if resolution.get("status") == "resolved" else None
    selected_year = resolution.get("product_year") if resolution.get("status") == "resolved" else None
    selected_keys = {
        (str(x.get("product_id")), str(x.get("product_year") or ""))
        for x in resolution.get("products", []) if isinstance(x, dict)
    }

    candidates = catalog.get("snapshots", []) if include_history else current_entries
    evidence: list[dict] = []
    leads: list[dict] = []
    filtered: list[dict] = []
    for meta in candidates:
        if not isinstance(meta, dict):
            continue
        kind = meta.get("资料类型")
        if kind not in TASKS[args.task]:
            filtered.append({"资料路径": meta.get("资料路径"), "reason": f"{TASK_LABELS[args.task]}不使用{kind}"})
            continue
        if kind == "产品资料":
            meta_is_lead = meta.get("qualification") == "lead" or meta.get("状态") in LEAD_STATUSES
            aliases = [str(meta.get("product_name") or meta.get("业务对象") or ""), *(meta.get("product_aliases") or [])]
            lead_matches = meta_is_lead and any(normalize(x) and normalize(x) in normalize(args.query) for x in aliases)
            if resolution.get("status") not in {"resolved", "resolved_multiple"} and not lead_matches:
                filtered.append({"reason": "产品未唯一确认，禁止全库混查"})
                continue
            matched = True if lead_matches else ((meta.get("product_id") == selected_product_id and not (selected_year and meta.get("product_year") and meta.get("product_year") != selected_year)) if resolution.get("status") == "resolved" else ((str(meta.get("product_id")), str(meta.get("product_year") or "")) in selected_keys))
            if not matched:
                filtered.append({"reason": "已排除一份其他产品／年份资料"})
                continue
        if meta.get("状态") in HISTORY_STATUSES and not include_history:
            filtered.append({"资料路径": meta.get("资料路径"), "reason": "默认排除历史／废弃资料"})
            continue
        page = pages.get(str(meta.get("sha256")))
        if not page:
            filtered.append({"资料路径": meta.get("资料路径"), "reason": "已生效目录有记录但来源快照缺失；建议运行检查／修复"})
            continue
        body = evidence_body(page["text"])
        snippets = {
            topic: snippet
            for topic in query_topics(args.query)
            if (snippet := best_snippet(body, topic, args.query))
        }
        content, covered = all_topic_snippets(body, args.query)
        item = {
            **meta,
            "page_path": page["page_path"],
            "content": content,
            "topic_snippets": snippets,
            "covered_topics": covered,
            "untrusted_source": True,
        }
        if meta.get("qualification") == "lead" or meta.get("状态") in LEAD_STATUSES:
            leads.append(item)
        else:
            evidence.append(item)

    evidence.sort(
        key=lambda x: (
            0 if x.get("资料类型") == "产品资料" else 1,
            authority_rank(str(x.get("证据级别", ""))),
            str(x.get("资料路径", "")),
        )
    )
    grouped: dict[str, list[dict]] = {kind: [] for kind in TASKS[args.task]}
    budget = 0
    per_kind_limit = 8 if args.task == "review" else 5
    for item in evidence:
        kind = item["资料类型"]
        if len(grouped[kind]) >= per_kind_limit:
            continue
        public_item = {k: v for k, v in item.items() if k != "product_aliases"}
        size = len(json.dumps(public_item, ensure_ascii=False).encode("utf-8"))
        if budget + size > 30 * 1024:
            continue
        grouped[kind].append(public_item)
        budget += size

    used_evidence = [item for values in grouped.values() for item in values]
    topics = query_topics(args.query)
    contradictions = contradiction_report(used_evidence, topics)
    topic_results = []
    for topic in topics:
        sources = [
            {
                "资料路径": item["资料路径"],
                "版本": item["版本"],
                "适用版本": item.get("applicable_version"),
                "证据级别": item["证据级别"],
                "原文片段": item.get("topic_snippets", {}).get(topic, "")[:1000],
            }
            for item in used_evidence if item.get("topic_snippets", {}).get(topic)
        ]
        conflict = any(x.get("主题") == topic and x.get("type", "conflict") == "conflict" for x in contradictions)
        topic_results.append({
            "主题": topic,
            "状态": "存在矛盾" if conflict else (
                "已找到" if sources else (
                    "检索尚不充分" if resolution.get("status") != "resolved" else "资料确未覆盖"
                )
            ),
            "证据": sources[:5],
        })

    pending_unregistered = list(catalog.get("pending_unregistered") or [])
    roots = scope.get("allowed_source_roots") or []
    if len(roots) == 1:
        try:
            from workspace_guard import inspect_workspace
            live = inspect_workspace(Path(roots[0]))
            pending_unregistered = live.get("unregistered_paths", pending_unregistered)
        except Exception:
            pass

    instructions = {
        "query": "只用合格产品证据和当前规则回答。按主题列路径、文档版本、适用版本与原文；冲突、检索不足和未覆盖必须分开说。",
        "generate": "先列产品事实、个人业务经验、个人表达参考，再生成草稿。生成后逐项核对数字、条件、例外和限定语，不能把用过样例等同于可直接用于业务。",
        "review": "按claim_checks逐项处理；已找到、检索尚不充分、资料确未覆盖、存在矛盾和需本人确认不得混用。必要时继续只读检索遗漏项。",
    }
    review = claim_review(args.query, used_evidence, resolution) if args.task == "review" else {"claim_checks": [], "review_summary": None}
    output = {
        "schema": "personal-kb.task-context.v2",
        "task": args.task,
        "task_label": TASK_LABELS[args.task],
        "query": args.query,
        "network_used_by_wrapper": False,
        "vault_mutation_allowed": False,
        "answer_instruction": instructions[args.task],
        "current_only_default": not include_history,
        "product_resolution": resolution,
        "contradictions": contradictions,
        "topic_results": topic_results,
        "claim_checks": review["claim_checks"],
        "review_summary": review["review_summary"],
        "leads": [
            {k: v for k, v in item.items() if k not in {"product_aliases", "topic_snippets"}}
            for item in leads[:6]
        ],
        "missing_groups": [kind for kind, values in grouped.items() if not values],
        "groups": grouped,
        "filtered": filtered,
        "maintenance_hint": (
            f"发现{len(pending_unregistered)}份待整理新资料；已有资料仍可查。需要时说‘更新我的知识库’。"
            if pending_unregistered else None
        ),
        "context_bytes_approx": budget,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
