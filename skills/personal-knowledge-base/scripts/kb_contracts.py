#!/usr/bin/env python3
"""Shared business contracts for both personal knowledge-base entry paths.

This module is deliberately deterministic.  It does not pretend to understand
arbitrary legal prose; it separates retrieval from verification and only marks
facts supported when a typed property can actually be compared.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from typing import Any

CURRENT_STATUSES = {"当前", "有效", "生效"}
LEAD_STATUSES = {"待核验", "待确认", "待本人确认"}
INACTIVE_STATUSES = {"历史", "废弃", "停用", "已停用", "失效"}
STATUS_LABELS = {
    "supported": "已找到",
    "contradicted": "存在矛盾",
    "missing_condition": "遗漏关键条件",
    "not_covered": "资料确未覆盖",
    "retrieval_incomplete": "检索尚不充分",
    "needs_semantic_review": "尚待语义核验",
    "needs_person_confirmation": "需本人确认",
    "non_factual": "非事实表达",
}


def normalize(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", value).casefold()
        if c.isalnum() or "\u3400" <= c <= "\u9fff"
    )


def status_bucket(status: str | None, qualification: str | None = None) -> str:
    value = str(status or "当前").strip()
    if value in INACTIVE_STATUSES:
        return "inactive"
    if value in LEAD_STATUSES or qualification == "lead":
        return "lead"
    if value in CURRENT_STATUSES and qualification != "lead":
        return "evidence"
    return "lead"


def is_current_evidence(item: dict) -> bool:
    return status_bucket(item.get("状态"), item.get("qualification")) == "evidence"


def is_lead(item: dict) -> bool:
    return status_bucket(item.get("状态"), item.get("qualification")) == "lead"


def _products(entries: list[dict]) -> dict[tuple[str, str], dict]:
    products: dict[tuple[str, str], dict] = {}
    for item in entries:
        if item.get("资料类型") != "产品资料" or not is_current_evidence(item):
            continue
        key = (
            str(item.get("product_id") or normalize(str(item.get("product_name") or item.get("业务对象", "")))),
            str(item.get("product_year") or ""),
        )
        record = products.setdefault(key, {
            "product_id": key[0],
            "product_name": item.get("product_name") or item.get("业务对象"),
            "product_year": key[1],
            "applicable_versions": set(),
            "aliases": set(),
        })
        record["applicable_versions"].add(str(item.get("applicable_version") or ""))
        record["aliases"].update(item.get("product_aliases") or [])
        record["aliases"].add(str(record["product_name"]))
    return products


def _public_product(record: dict) -> dict:
    return {
        "product_id": record["product_id"],
        "product_name": record["product_name"],
        "product_year": record["product_year"],
        "applicable_versions": sorted(x for x in record["applicable_versions"] if x),
    }


def _explicit_target_phrases(query: str) -> list[str]:
    text = unicodedata.normalize("NFKC", query)
    values: list[str] = []
    patterns = (
        r"([A-Za-z0-9\u3400-\u9fff（）()·_-]{2,40}?)(?:的)?(?:保单贷款|贷款|红利|分红|保险责任|条款|投保|领取)",
        r"(?:查询|审核|核对|关于|请问|看看)\s*([A-Za-z0-9\u3400-\u9fff（）()·_-]{2,30}(?:年金|寿险|医疗险|重疾险|保险)(?:\d{4})?)",
    )
    stop_prefix = re.compile(r"^(?:调用我的知识库|调用知识库|结合我的知识库|请问|请|查询|审核|核对|关于|改问)+")
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = stop_prefix.sub("", match.group(1)).strip(" ：:，,。；;“”'\"")
            # Avoid treating generic object nouns as a named product.
            if value and normalize(value) not in {"这个产品", "该产品", "产品", "保险", "保单"}:
                values.append(value)
    return values


def _match_target(target: dict, products: dict[tuple[str, str], dict]) -> list[dict]:
    wanted_id = normalize(str(target.get("product_id") or ""))
    wanted_name = normalize(str(target.get("name") or target.get("product_name") or ""))
    wanted_year = str(target.get("year") or target.get("product_year") or "")
    matched: list[dict] = []
    for record in products.values():
        identities = {normalize(str(record["product_id"])), normalize(str(record["product_name"]))}
        identities.update(normalize(str(x)) for x in record["aliases"])
        if (wanted_id and wanted_id in identities) or (wanted_name and wanted_name in identities):
            if not wanted_year or not record["product_year"] or wanted_year == record["product_year"]:
                matched.append(record)
    return matched


def product_resolution(query: str, entries: list[dict], target_intent: dict | None = None) -> dict:
    """Resolve product intent without substituting a different library product."""
    products = _products(entries)
    nq = normalize(query)
    intent = target_intent if isinstance(target_intent, dict) else None
    if intent and intent.get("mode") == "not_required":
        return {"status": "not_required", "intent": "not_required", "reason": "宿主Agent确认本次任务不涉及产品事实。"}
    if intent and intent.get("mode") in {"explicit", "confirmed"}:
        targets = intent.get("targets") if isinstance(intent.get("targets"), list) else []
        if intent.get("mode") == "confirmed" and not targets:
            targets = [{"product_id": intent.get("product_id"), "year": intent.get("product_year")}]
        resolved: list[dict] = []
        missing: list[dict] = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            values = _match_target(target, products)
            if len(values) == 1:
                resolved.append(values[0])
            else:
                missing.append(target)
        if missing or not resolved:
            return {
                "status": "not_found" if missing else "needs_confirmation",
                "intent": "structured_explicit",
                "requested_objects": targets,
                "unmatched_objects": missing,
                "reason": "明确目标未全部匹配当前合格资料；未借用其他产品。",
                "choices": [_public_product(record) for record in products.values()],
            }
        unique = {(x["product_id"], x["product_year"]): x for x in resolved}
        if intent.get("mode") == "confirmed":
            explicit_phrases = _explicit_target_phrases(query)
            declared_names = {normalize(str(x["product_name"])) for x in unique.values()}
            declared_names.update(normalize(str(alias)) for x in unique.values() for alias in x["aliases"])
            named_records = []
            for record in products.values():
                if any(normalize(str(alias)) in nq for alias in record["aliases"] if len(normalize(str(alias))) >= 2):
                    named_records.append(record)
            mismatched_known = [x for x in named_records if (x["product_id"], x["product_year"]) not in unique]
            mismatched_unknown = [x for x in explicit_phrases if not any(normalize(x) == name or normalize(x) in name or name in normalize(x) for name in declared_names)]
            if mismatched_known or mismatched_unknown:
                return {
                    "status": "not_found" if mismatched_unknown else "needs_confirmation",
                    "intent": "confirmed_object_overridden_by_new_explicit_object",
                    "requested_objects": explicit_phrases,
                    "reason": "新问题出现了不同的明确对象，旧的会话确认未被沿用。",
                    "choices": [_public_product(record) for record in products.values()],
                }
        if len(unique) == 1:
            record = next(iter(unique.values()))
            primary = normalize(str(record["product_name"]))
            matched_aliases = sorted(
                str(alias) for alias in record["aliases"]
                if normalize(str(alias)) != primary
                and len(normalize(str(alias))) >= 2
                and normalize(str(alias)) in nq
            )
            return {
                "status": "resolved",
                "intent": "structured_explicit",
                **_public_product(record),
                "matched_aliases": matched_aliases,
            }
        return {
            "status": "resolved_multiple",
            "intent": "structured_multiple",
            "products": [_public_product(x) for x in unique.values()],
            "reason": "多个明确目标已分别匹配；调用方必须分产品呈现证据。",
        }
    if intent and intent.get("mode") == "unspecified":
        return {
            "status": "needs_confirmation",
            "intent": "structured_unspecified",
            "reason": "用户未指定产品，请用普通语言确认一次查询对象。",
            "choices": [_public_product(record) for record in products.values()],
        }
    query_years = set(re.findall(r"(?:19|20)\d{2}", unicodedata.normalize("NFKC", query)))
    matches: dict[tuple[str, str], tuple[dict, list[str]]] = {}
    for key, record in products.items():
        aliases = sorted((str(x) for x in record["aliases"] if len(normalize(str(x))) >= 2), key=lambda x: len(normalize(x)), reverse=True)
        found = [alias for alias in aliases if normalize(alias) in nq]
        if found and not (query_years and record["product_year"] and record["product_year"] not in query_years):
            matches[key] = (record, found)
    if len(matches) == 1:
        record, found = next(iter(matches.values()))
        return {
            "status": "resolved",
            "intent": "explicit_match",
            **_public_product(record),
            "matched_aliases": found,
            "matched_alias": found[0],
        }
    if len(matches) > 1:
        return {
            "status": "needs_confirmation",
            "intent": "multiple_products",
            "reason": "问题同时命中多个产品／年份，未静默选择其中一个。",
            "choices": [_public_product(value[0]) for value in matches.values()],
        }
    explicit = _explicit_target_phrases(query)
    if explicit:
        partial_choices = []
        for record in products.values():
            identities = {normalize(str(record["product_name"])), *(normalize(str(x)) for x in record["aliases"])}
            if any(normalize(target) and any(identity.startswith(normalize(target)) or normalize(target).startswith(identity) for identity in identities) for target in explicit):
                partial_choices.append(record)
        if len(partial_choices) > 1:
            return {
                "status": "needs_confirmation",
                "intent": "ambiguous_partial_name",
                "requested_objects": explicit,
                "reason": "名称同时可能指向多个当前产品／年份，请选择。",
                "choices": [_public_product(record) for record in partial_choices],
            }
        return {
            "status": "not_found",
            "intent": "explicit_unknown",
            "requested_objects": explicit,
            "reason": "问题明确指定了对象，但知识库没有对应的当前合格资料；未使用其他产品替代。",
            "choices": [_public_product(record) for record in products.values()],
        }
    if not products:
        return {"status": "not_found", "intent": "unspecified", "reason": "知识库没有当前合格产品资料。", "choices": []}
    return {
        "status": "needs_confirmation",
        "intent": "unspecified",
        "reason": "纯文本问题没有可靠识别到明确产品，请选择；不会因库内只有一个产品而自动代入。",
        "choices": [_public_product(record) for record in products.values()],
    }


def chinese_number(value: str) -> int | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "百" in value:
        left, right = value.split("百", 1)
        total = digits.get(left, 1) * 100
        if right:
            tail = chinese_number(right)
            return total + (tail or 0)
        return total
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    if all(x in digits for x in value):
        try:
            return int("".join(str(digits[x]) for x in value))
        except ValueError:
            return None
    return None


def semantic_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value)
    def repl(match: re.Match[str]) -> str:
        number = chinese_number(match.group(1))
        return match.group(0) if number is None else f"{number}%"
    text = re.sub(r"百分之([零〇一二两三四五六七八九十百\d]+)", repl, text)
    return re.sub(r"\s+", "", text)


def _sentences(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text)
    values = [x.strip() for x in re.split(r"[。！？!?；;\n]+", normalized) if x.strip()]
    return values or ([normalized.strip()] if normalized.strip() else [])


def _sentence_spans(text: str) -> list[tuple[str, int, int]]:
    normalized = unicodedata.normalize("NFKC", text)
    result: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^。！？!?；;\n]+", normalized):
        value = match.group(0).strip()
        if value:
            result.append((value, match.start(), match.end()))
    return result or ([(normalized.strip(), 0, len(normalized))] if normalized.strip() else [])


def _percentages(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)%", semantic_text(text))]


def _months(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"(\d+)个?月", semantic_text(text))]


def _ages(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"(\d+)周岁", semantic_text(text))]


def _personal(text: str) -> bool:
    return bool(re.search(r"我会|我通常|我的客户|我服务|本人(?:会|通常|曾|负责)|个人经历|服务流程", text))


def atomize_claims(text: str) -> tuple[list[dict], list[dict]]:
    """Return parent claims and independently verifiable atomic facts."""
    parents: list[dict] = []
    atoms: list[dict] = []
    for index, (sentence, start, end) in enumerate(_sentence_spans(text), 1):
        parent = {"parent_id": index, "text": sentence, "start": start, "end": end}
        parents.append(parent)
        compact = semantic_text(sentence)
        topic = "保单贷款" if any(x in compact for x in ("贷款", "现金价值", "欠款", "合同效力中止")) else (
            "红利" if any(x in compact for x in ("红利", "分红")) else "综合问题"
        )
        start_count = len(atoms)
        if _personal(sentence):
            atoms.append({"parent_id": index, "parent_text": sentence, "fact_class": "个人经历", "topic": topic, "property": "personal_experience", "value": None})
        if topic == "保单贷款":
            percentages = _percentages(compact)
            if percentages and any(x in compact for x in ("上限", "不得超过", "贷款金额", "可贷")):
                basis = "net_of_arrears_interest" if re.search(r"(?:扣除|减去).{0,12}(?:各项)?欠款.{0,8}利息", compact) else (
                    "gross_cash_value" if "不扣除" in compact or "无需扣除" in compact else None
                )
                for value in percentages:
                    atoms.append({"parent_id": index, "parent_text": sentence, "fact_class": "产品事实", "topic": topic, "property": "loan_limit_percent", "value": value, "unit": "%", "conditions": {"calculation_basis": basis}})
            if "期限" in compact:
                for value in _months(compact):
                    atoms.append({"parent_id": index, "parent_text": sentence, "fact_class": "产品事实", "topic": topic, "property": "loan_term_months", "value": value, "unit": "个月", "conditions": {"per_loan": "每次" in compact}})
            if "效力中止" in compact or "合同中止" in compact:
                conditions = {
                    "loan_principal_interest": bool(re.search(r"(?:贷款)?本金.{0,5}利息|本息", compact)),
                    "other_unpaid_items": "其他未还款项" in compact,
                    "cash_value_threshold": "现金价值" in compact and "达到" in compact,
                }
                atoms.append({"parent_id": index, "parent_text": sentence, "fact_class": "产品事实", "topic": topic, "property": "loan_termination_condition", "value": "contract_suspended", "conditions": conditions})
        if topic == "红利":
            guaranteed: bool | None = None
            if re.search(r"不保证|非保证|不能保证|无法保证|可能为零|可以为零", compact):
                guaranteed = False
            elif re.search(r"保证(?:发放|分配|获得|有)|一定(?:发放|有)|不会为零|不可能为零", compact):
                guaranteed = True
            if guaranteed is not None:
                atoms.append({"parent_id": index, "parent_text": sentence, "fact_class": "产品事实", "topic": topic, "property": "dividend_guaranteed", "value": guaranteed, "conditions": {}})
        if any(x in compact for x in ("投保年龄", "年龄范围")):
            for value in _ages(compact):
                atoms.append({"parent_id": index, "parent_text": sentence, "fact_class": "产品事实", "topic": "投保年龄", "property": "insured_age", "value": value, "unit": "周岁", "conditions": {}})
        if len(atoms) == start_count:
            # Coverage is lossless.  Potentially factual prose is routed to the
            # host Agent instead of being mislabeled as a retrieval failure.
            factual = bool(re.search(r"(?:保险|保单|合同|产品|条款|贷款|红利|分红|投保|领取|给付|赔付|保证|不得|可以|应当|必须|属于|为零)", compact))
            atoms.append({"parent_id": index, "parent_text": sentence, "fact_class": "未分类事实" if factual else "非事实表达", "topic": topic, "property": "unclassified_statement" if factual else "unstructured_statement", "value": None, "conditions": {}})
        for atom in atoms[start_count:]:
            atom.update({"source_start": start, "source_end": end, "comparison": "equals", "polarity": "negative" if re.search(r"不|未|无|非", compact) else "affirmative"})
    return parents, atoms


def _atom_key(atom: dict) -> tuple[str, Any]:
    return str(atom.get("property")), atom.get("value")


def _source_atoms(text: str) -> list[dict]:
    _, atoms = atomize_claims(text)
    return [x for x in atoms if x.get("fact_class") == "产品事实"]


def verify_claims(query: str, sources: list[dict], resolution: dict) -> dict:
    parents, atoms = atomize_claims(query)
    source_cache: list[tuple[dict, dict, str]] = []
    for source in sources:
        for chunk in source.get("chunks", []):
            text = str(chunk.get("text", ""))
            for atom in _source_atoms(text):
                source_cache.append((source, atom, text))
    checks: list[dict] = []
    for atom_index, atom in enumerate(atoms, 1):
        status = "retrieval_incomplete"
        difference = ""
        evidence: list[dict] = []
        if atom["fact_class"] == "个人经历":
            status = "needs_person_confirmation"
            difference = "这是本人经历或服务流程，产品条款不能替本人确认。"
        elif atom["fact_class"] == "非事实表达":
            status = "non_factual"
            difference = "该句未识别为可由产品资料判真的具体事实。"
        elif atom["fact_class"] == "未分类事实":
            if resolution.get("status") not in {"resolved", "resolved_multiple"} or not sources:
                status = "retrieval_incomplete"
                difference = "尚未取得明确产品的完整合格证据。"
            else:
                status = "needs_semantic_review"
                difference = "证据已提供，但该事实超出确定性比较域，需当前宿主Agent结合原文完成语义核验。"
        elif resolution.get("status") != "resolved":
            status = "retrieval_incomplete"
            difference = "产品或适用范围未唯一确认，未跨产品核验。"
        else:
            candidates = [(s, a, t) for s, a, t in source_cache if a.get("property") == atom.get("property")]
            for source, source_atom, text in candidates[:8]:
                chunk = next((x for x in source.get("chunks", []) if str(x.get("text", "")) == text), {})
                evidence.append({
                    "file": source.get("source_relative") or source.get("资料路径"),
                    "document_id": source.get("document_id"),
                    "sha256": source.get("sha256"),
                    "document_version": source.get("document_version") or source.get("版本"),
                    "applicable_version": source.get("applicable_version"),
                    "evidence_level": source.get("证据级别"),
                    "chunk_id": chunk.get("chunk_id"),
                    "text": text[:1200],
                    "source_value": source_atom.get("value"),
                    "source_conditions": source_atom.get("conditions", {}),
                })
            if not candidates:
                # A resolved product with all its current chunks inspected is a bounded not-covered result.
                status = "not_covered" if sources else "retrieval_incomplete"
                difference = "已检查该产品当前合格资料，未找到可比较的同属性事实。" if sources else "没有可核验的当前合格资料。"
            else:
                same = [(s, a, t) for s, a, t in candidates if a.get("value") == atom.get("value")]
                if not same:
                    status = "contradicted"
                    values = sorted({str(a.get("value")) for _, a, _ in candidates})
                    difference = f"待审值为{atom.get('value')}，当前资料同属性值为{'、'.join(values)}。"
                else:
                    status = "supported"
                    if atom.get("property") == "loan_limit_percent":
                        source_basis = {a.get("conditions", {}).get("calculation_basis") for _, a, _ in same}
                        claim_basis = atom.get("conditions", {}).get("calculation_basis")
                        if claim_basis == "gross_cash_value" and "net_of_arrears_interest" in source_basis:
                            status = "contradicted"
                            difference = "比例相同，但计算基数与资料相反。"
                        elif "net_of_arrears_interest" in source_basis and claim_basis != "net_of_arrears_interest":
                            status = "missing_condition"
                            difference = "比例相同，但遗漏了“现金价值扣除各项欠款及利息后余额”这一计算基数。"
                    elif atom.get("property") == "loan_term_months":
                        if any(a.get("conditions", {}).get("per_loan") for _, a, _ in same) and not atom.get("conditions", {}).get("per_loan"):
                            status = "missing_condition"
                            difference = "期限数值相同，但遗漏“每次贷款”的适用条件。"
                    elif atom.get("property") == "loan_termination_condition":
                        required = {k for _, a, _ in same for k, v in a.get("conditions", {}).items() if v}
                        supplied = {k for k, v in atom.get("conditions", {}).items() if v}
                        missing = sorted(required - supplied)
                        if missing:
                            labels = {"loan_principal_interest": "贷款本金及利息", "other_unpaid_items": "其他未还款项", "cash_value_threshold": "达到现金价值"}
                            status = "missing_condition"
                            difference = "遗漏关键触发条件：" + "、".join(labels[x] for x in missing)
        checks.append({
            "atom_id": atom_index,
            "parent_id": atom["parent_id"],
            "parent_text": atom["parent_text"],
            "claim": atom["parent_text"],
            "fact_class": atom["fact_class"],
            "topic": atom["topic"],
            "property": atom["property"],
            "claimed_value": atom.get("value"),
            "claimed_conditions": atom.get("conditions", {}),
            "source_start": atom.get("source_start"),
            "source_end": atom.get("source_end"),
            "comparison": atom.get("comparison"),
            "polarity": atom.get("polarity"),
            "verification_method": "deterministic_property_compare" if atom["fact_class"] == "产品事实" else ("host_agent_semantic_review" if atom["fact_class"] == "未分类事实" else "classification"),
            "verification_status": status,
            "status": STATUS_LABELS[status],
            "difference": difference,
            "evidence": evidence[:4],
        })
    unresolved = [x["atom_id"] for x in checks if x["verification_status"] in {"retrieval_incomplete", "needs_semantic_review", "needs_person_confirmation"}]
    failed = [x["atom_id"] for x in checks if x["verification_status"] in {"contradicted", "missing_condition"}]
    summary = {
        "submitted_parent_count": len(parents),
        "atomic_fact_count": len(atoms),
        "reviewed_atomic_count": len(checks),
        "unresolved_atomic_count": len(unresolved),
        "unresolved_atom_ids": unresolved,
        "failed_atom_ids": failed,
        "input_id": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "complete": len(checks) == len(atoms) and not unresolved,
        "all_supported": bool(checks) and all(x["verification_status"] in {"supported", "non_factual"} for x in checks),
    }
    return {"parents": parents, "claim_checks": checks, "review_summary": summary}


def conflict_report(sources: list[dict]) -> list[dict]:
    """Compare values of the same typed property, not whole numeric sets."""
    grouped: dict[tuple[str, str, str, str], list[tuple[dict, dict, str]]] = defaultdict(list)
    loan_basis_groups: dict[tuple[str, str, str], list[tuple[dict, dict, str]]] = defaultdict(list)
    for source in sources:
        if source.get("资料类型") != "产品资料":
            continue
        text = "\n".join(str(x.get("text", "")) for x in source.get("chunks", []))
        for atom in _source_atoms(text):
            if atom.get("property") == "unstructured_statement":
                continue
            basis = atom.get("conditions", {}).get("calculation_basis") if atom.get("property") == "loan_limit_percent" else None
            key = (str(source.get("product_id")), str(source.get("applicable_version")), str(atom.get("property")), str(basis or "unspecified"))
            grouped[key].append((source, atom, text))
            if atom.get("property") == "loan_limit_percent":
                loan_basis_groups[(key[0], key[1], key[2])].append((source, atom, text))
    result: list[dict] = []
    for (product_id, applicable, prop, basis), values in grouped.items():
        distinct = {str(atom.get("value")) for _, atom, _ in values}
        conflicting = len(distinct) > 1
        if not conflicting:
            continue
        claims = []
        seen = set()
        for source, atom, _ in values:
            signature = (source.get("sha256"), atom.get("value"), str(atom.get("conditions")))
            if signature in seen:
                continue
            seen.add(signature)
            claims.append({
                "file": source.get("source_relative") or source.get("资料路径"),
                "document_id": source.get("document_id"),
                "evidence_level": source.get("证据级别"),
                "value": atom.get("value"),
                "conditions": atom.get("conditions", {}),
            })
        result.append({
            "type": "conflict",
            "product_id": product_id,
            "applicable_version": applicable,
            "property": prop,
            "calculation_basis": basis,
            "主题": "保单贷款" if prop.startswith("loan_") else prop,
            "claims": claims,
            "action": "同一适用范围的同属性结论不一致；保留正式依据，确认前不合并。",
        })
    for (product_id, applicable, prop), values in loan_basis_groups.items():
        by_value: dict[str, list[tuple[dict, dict, str]]] = defaultdict(list)
        for value in values:
            by_value[str(value[1].get("value"))].append(value)
        for same_values in by_value.values():
            bases = {str(x[1].get("conditions", {}).get("calculation_basis") or "unspecified") for x in same_values}
            if len(bases) <= 1:
                continue
            result.append({
                "type": "condition_difference",
                "product_id": product_id,
                "applicable_version": applicable,
                "property": prop,
                "主题": "保单贷款",
                "reason": "数值相同但计算基数不同，不能直接合并为支持或矛盾。",
                "claims": [{"file": x[0].get("source_relative") or x[0].get("资料路径"), "value": x[1].get("value"), "conditions": x[1].get("conditions", {})} for x in same_values],
            })
    return result
