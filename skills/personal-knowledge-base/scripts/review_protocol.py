#!/usr/bin/env python3
"""Deterministic review protocol for the five closed-domain product facts.

The module deliberately keeps raw user offsets separate from normalized text,
binds host review plans to one content/control snapshot, and treats semantic
results as untrusted input that must cite an eligible current revision exactly.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
import hashlib
import json
import re
import unicodedata
from typing import Any


STATUS_LABELS = {
    "supported": "已找到",
    "contradicted": "存在矛盾",
    "missing_condition": "遗漏关键条件",
    "not_covered": "资料确未覆盖",
    "retrieval_incomplete": "检索尚不充分",
    "needs_semantic_review": "尚待语义核验",
    "needs_person_confirmation": "需本人确认",
    "conflict": "资料冲突",
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def prepare_contract(text: str, snapshot_id: str) -> dict:
    segments = []
    for match in re.finditer(r"[^。！？!?；;\n]+[。！？!?；;\n]*|[。！？!?；;\n]+", text):
        segments.append({"id": f"s{len(segments)+1}", "start": match.start(), "end": match.end(), "text": match.group()})
    return {
        "schema": "personal-kb.review-contract.v1",
        "input_id": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "snapshot_id": snapshot_id,
        "input_text": text,
        "segments": segments,
    }


def validate_plan(contract: dict, plan: Any) -> dict:
    errors: list[str] = []
    if not isinstance(plan, dict):
        return {"valid": False, "errors": ["plan_object_required"]}
    if set(plan) - {"schema", "input_id", "snapshot_id", "units", "atoms", "coverage_audit"}:
        errors.append("unknown_plan_fields")
    if plan.get("schema") != "personal-kb.review-plan.v1":
        errors.append("plan_schema")
    for key in ("input_id", "snapshot_id"):
        if plan.get(key) != contract.get(key):
            errors.append("stale_" + key)
    text = str(contract.get("input_text", ""))
    segs = {s["id"]: s for s in contract.get("segments", []) if isinstance(s, dict) and isinstance(s.get("id"), str)}
    units, atoms, audit = plan.get("units"), plan.get("atoms"), plan.get("coverage_audit")
    if not isinstance(units, list) or not isinstance(atoms, list) or not isinstance(audit, list):
        return {"valid": False, "errors": errors + ["plan_arrays_required"]}
    unit_ids = [u.get("id") for u in units if isinstance(u, dict)]
    atom_ids = [a.get("id") for a in atoms if isinstance(a, dict)]
    if len(unit_ids) != len(units) or any(not isinstance(x, str) or not x for x in unit_ids) or len(set(unit_ids)) != len(unit_ids):
        errors.append("unit_ids")
    if len(atom_ids) != len(atoms) or any(not isinstance(x, str) or not x for x in atom_ids) or len(set(atom_ids)) != len(atom_ids):
        errors.append("atom_ids")
    if "unit_ids" in errors or "atom_ids" in errors:
        return {"valid": False, "errors": errors}
    atom_map = {a["id"]: a for a in atoms}
    referenced: set[str] = set()
    intervals: list[tuple[int, int, str]] = []
    unresolved = []
    for unit in units:
        uid = unit["id"]
        if set(unit) - {"id", "segment_id", "start", "end", "quote", "kind", "atom_ids", "reason"}:
            errors.append("unknown_unit_fields:" + uid)
        start, end = unit.get("start"), unit.get("end")
        segment = segs.get(unit.get("segment_id"))
        if type(start) is not int or type(end) is not int or not (0 <= start < end <= len(text)):
            errors.append("unit_span:" + uid)
            continue
        if segment is None or not (segment["start"] <= start < end <= segment["end"]):
            errors.append("unit_segment:" + uid)
        if unit.get("quote") != text[start:end]:
            errors.append("unit_quote:" + uid)
        intervals.append((start, end, uid))
        kind, refs = unit.get("kind"), unit.get("atom_ids")
        if kind not in {"fact", "personal", "non_factual", "unresolved"}:
            errors.append("unit_kind:" + uid)
        if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs) or len(set(refs)) != len(refs):
            errors.append("unit_atom_ids:" + uid)
            continue
        if kind in {"fact", "personal"} and not refs:
            errors.append("unit_missing_atom:" + uid)
        if kind in {"non_factual", "unresolved"} and refs:
            errors.append("unit_unexpected_atom:" + uid)
        if kind in {"non_factual", "unresolved"} and not str(unit.get("reason") or "").strip():
            errors.append("unit_reason:" + uid)
        if kind == "unresolved":
            unresolved.append(uid)
        for ref in refs:
            atom = atom_map.get(ref)
            if atom is None or uid not in atom.get("unit_ids", []):
                errors.append("unit_atom_link:" + uid)
                continue
            if atom.get("fact_class") != ("personal" if kind == "personal" else "product"):
                errors.append("unit_fact_class:" + uid)
            referenced.add(ref)
    cursor = 0
    for start, end, uid in sorted(intervals):
        if start != cursor:
            errors.append(("coverage_overlap:" if start < cursor else "coverage_gap:") + uid)
        cursor = max(cursor, end)
    if cursor != len(text):
        errors.append("coverage_tail_gap")
    if referenced != set(atom_ids):
        errors.append("orphan_atom")
    unit_map = {u["id"]: u for u in units}
    for atom in atoms:
        aid = atom["id"]
        if set(atom) - {"id", "unit_ids", "fact_class", "product_id", "applicable_version", "property", "operator", "polarity", "value", "unit", "conditions"}:
            errors.append("unknown_atom_fields:" + aid)
        refs = atom.get("unit_ids")
        if not isinstance(refs, list) or not refs or any(not isinstance(u, str) or u not in unit_map or aid not in unit_map[u].get("atom_ids", []) for u in refs):
            errors.append("atom_unit_link:" + aid)
        if atom.get("fact_class") == "product":
            for key in ("product_id", "applicable_version", "property", "operator", "polarity", "unit"):
                if not isinstance(atom.get(key), str) or not atom[key]:
                    errors.append("atom_" + key + ":" + aid)
            if "value" not in atom or not isinstance(atom.get("conditions"), dict):
                errors.append("atom_value_conditions:" + aid)
            if atom.get("polarity") not in {"affirmative", "negative"}:
                errors.append("atom_polarity:" + aid)
            if atom.get("operator") not in {"maximum", "minimum", "equals"}:
                errors.append("atom_operator:" + aid)
            if atom.get("unit") not in {"percent", "month", "year", "boolean", "none"}:
                errors.append("atom_unit:" + aid)
            if atom.get("value") is not None and type(atom.get("value")) not in (str, bool):
                errors.append("atom_value_type:" + aid)
    audit_ids = [x.get("segment_id") for x in audit if isinstance(x, dict)]
    if len(audit_ids) != len(audit) or any(not isinstance(x, str) for x in audit_ids) or len(set(audit_ids)) != len(audit_ids) or set(audit_ids) != set(segs):
        errors.append("audit_segments")
    for row in audit:
        if not isinstance(row, dict):
            continue
        if set(row) - {"segment_id", "atom_ids", "reviewed_full_segment", "omissions"}:
            errors.append("unknown_audit_fields")
        sid = row.get("segment_id")
        expected = {ref for u in units if u.get("segment_id") == sid for ref in u.get("atom_ids", [])}
        got = row.get("atom_ids")
        if not isinstance(got, list) or any(not isinstance(x, str) for x in got) or len(set(got)) != len(got) or set(got) != expected:
            errors.append("audit_atoms:" + str(sid))
        if row.get("reviewed_full_segment") is not True or not isinstance(row.get("omissions"), list):
            errors.append("audit_incomplete:" + str(sid))
        elif row["omissions"]:
            errors.append("audit_omissions:" + str(sid))
    return {"valid": not errors, "errors": errors, "structural_complete": not errors, "unresolved_unit_ids": unresolved}


def _chinese_integer(text: str) -> int | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, right = text.split("十", 1)
        if left in ("", *digits) and right in ("", *digits):
            return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(text)


def _normalize(text: str) -> str:
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    def percent(match: re.Match[str]) -> str:
        number = _chinese_integer(match.group(1))
        return match.group(0) if number is None else f"{number}%"
    value = re.sub(r"百分之([零〇一二两三四五六七八九十\d]+)", percent, value)
    def months(match: re.Match[str]) -> str:
        number = _chinese_integer(match.group(1))
        return match.group(0) if number is None else f"{number}个月"
    return re.sub(r"([零〇一二两三四五六七八九十]+)个月", months, value)


def _decimal(text: str) -> str:
    return format(Decimal(text).normalize(), "f")


def _fact(prop: str, value: str | bool | None, unit: str, operator: str = "equals", polarity: str = "affirmative", conditions: dict | None = None) -> dict:
    return {"property": prop, "value": value, "unit": unit, "operator": operator, "polarity": polarity, "conditions": conditions or {}}


def parse_clause(quote: str, product_aliases: tuple[str, ...] = ()) -> list[dict] | None:
    text = _normalize(quote).strip("。；;!?！？,，")
    for alias in sorted((_normalize(x) for x in product_aliases), key=len, reverse=True):
        if alias and text.startswith(alias):
            text = text[len(alias):].removeprefix("的")
            break
    # Enumeration is document structure; retain the raw span in compile_text
    # while normalizing only an anchored, explicit list label here.
    text = re.sub(r"^第[零〇一二两三四五六七八九十百\d]+项[:：]", "", text)
    num = r"(\d+(?:\.\d+)?)"
    age = re.fullmatch(r"(?:被保险人)?(?:的)?投保年龄(?:为|是)?" + num + r"周岁", text)
    if age:
        return [_fact("insured_age_years", _decimal(age.group(1)), "year")]
    term = re.fullmatch(r"(每?次)?(?:保单)?贷款(?:的)?期限(最长不超过|最长|最短|不得超过|不超过|不少于|不可能为|不能为)(?:为|是)?" + num + r"个?月", text)
    if term:
        each, op, value = term.groups()
        operator = {"最长不超过": "maximum", "最长": "maximum", "最短": "minimum", "不得超过": "maximum", "不超过": "maximum", "不少于": "minimum", "不可能为": "equals", "不能为": "equals"}[op]
        return [_fact("loan_term_months", _decimal(value), "month", operator, "negative" if op in {"不可能为", "不能为"} else "affirmative", {"per_loan": True} if each else {})]
    plain_term = re.fullmatch(r"(每次)?(?:保单)?(?:贷款)?期限(?:为|是)?" + num + r"个?月", text)
    if plain_term:
        each, value = plain_term.groups()
        return [_fact("loan_term_months", _decimal(value), "month", "equals", conditions={"per_loan": True} if each else {})]
    limit = re.fullmatch(r"(?:保单)?贷款(?:金额)?(?:上限(?:为|是)?|不得超过|不超过)(?:(申请时)?现金价值(扣除各项欠款及利息后余额)?的)?" + num + "%", text)
    if limit:
        at_application, net, value = limit.groups()
        conditions: dict[str, str] = {}
        if at_application:
            conditions["valuation_time"] = "at_application"
        if net:
            conditions["calculation_basis"] = "net_of_arrears_interest"
        elif "现金价值" in text:
            conditions["calculation_basis"] = "gross_cash_value"
        return [_fact("loan_limit_percent", _decimal(value), "percent", "maximum", conditions=conditions)]
    detailed_limit = re.fullmatch(r"(?:贷款)?金额(?:上限)?(?:为|是|不得超过|不超过)(?:申请(?:贷款)?时)?(?:本合同)?现金价值(扣除各项欠款及利息后余额)?的?" + num + "%", text)
    if detailed_limit:
        net, value = detailed_limit.groups()
        conditions = {"valuation_time": "at_application"} if "申请" in text else {}
        conditions["calculation_basis"] = "net_of_arrears_interest" if net else "gross_cash_value"
        return [_fact("loan_limit_percent", _decimal(value), "percent", "maximum", conditions=conditions)]
    opposite = re.fullmatch(r"(?:保单)?贷款金额(?:无需扣除|不扣除)(?:各项)?欠款(?:及利息)?[,，]?(?:上限为|为)现金价值的" + num + "%", text)
    if opposite:
        return [_fact("loan_limit_percent", _decimal(opposite.group(1)), "percent", "maximum", conditions={"calculation_basis": "gross_cash_value"})]
    suspension = re.fullmatch(r"(?:当|若)(贷款本金及利息|贷款本息)(加其他未还款项)?达到现金价值时[,，]?(不会|不)?(?:导致)?(?:本)?合同(?:效力)?中止", text)
    if suspension:
        _, other, negative = suspension.groups()
        conditions = {"loan_principal_interest": True, "cash_value_threshold": True}
        if other:
            conditions["other_unpaid_items"] = True
        return [_fact("loan_termination_condition", not bool(negative), "boolean", conditions=conditions)]
    dividend = re.fullmatch(r"(?:保单)?(?:红利|分红)(?:为|是)?(不保证|非保证|不能保证|保证发放|保证分配|保证获得)(?:的)?(?:[,，]?(?:也|但(?:一定)?|而且|且|并且)?(可能为零|可以为零|不会为零|不可能为零))?", text)
    if dividend:
        guarantee, zero = dividend.groups()
        result = [_fact("dividend_guaranteed", guarantee not in {"不保证", "非保证", "不能保证"}, "boolean")]
        if zero:
            result.append(_fact("dividend_zero_possible", zero in {"可能为零", "可以为零"}, "boolean"))
        return result
    fixed_dividend = re.fullmatch(r"(?:保单)?(?:红利|分红)(?:固定|保证)(?:为|是)?" + num + r"%", text)
    if fixed_dividend:
        # Guarantee is deterministic, but a dividend-rate percentage is outside
        # the closed comparison domain.  Keep the numeric proposition as a
        # separate semantic obligation instead of silently discarding it.
        return [_fact("dividend_guaranteed", True, "boolean"), _fact("unclassified_statement", None, "none")]
    zero = re.fullmatch(r"(?:保单)?(?:红利|分红)(可能为零|可以为零|不会为零|不可能为零)", text)
    if zero:
        return [_fact("dividend_zero_possible", zero.group(1) in {"可能为零", "可以为零"}, "boolean")]
    return None


def compile_text(text: str, aliases: tuple[str, ...] = ()) -> list[dict]:
    """Losslessly split text while preserving exact code-point offsets."""
    units: list[dict] = []
    def emit(start: int, end: int, kind: str, facts: list[dict] | None = None, reason: str | None = None) -> None:
        if start != end:
            units.append({"start": start, "end": end, "quote": text[start:end], "kind": kind, "facts": facts or [], "reason": reason})
    def parse_span(start: int, end: int) -> None:
        raw = text[start:end]
        # Distinguish structural topic labels from assertions. A Markdown
        # heading containing a number/condition/claim is still parsed in full.
        heading = re.match(r"^[ \t]*#{1,6}[ \t]+", raw)
        if heading:
            label = re.sub(r"^\d+(?:\.\d+)*[ \t]+", "", raw[heading.end():].strip())
            if label in {"保单贷款", "贷款", "保单红利", "红利", "分红", "投保范围", "投保年龄", "合同效力", "保险责任", "保单现金价值", *aliases}:
                emit(start, end, "non_factual", reason="structural_heading")
                return
            emit(start, start+heading.end(), "non_factual", reason="structural_markup")
            parse_span(start+heading.end(), end)
            return
        normalized = _normalize(raw).strip("。；;!?！？,，")
        if re.match(r"^(?:我会|我通常|我的客户|我服务|个人经历)", normalized):
            emit(start, end, "personal", [_fact("personal_experience", None, "none")])
            return
        if re.fullmatch(r"(?:欢迎关注|欢迎点赞|欢迎转发|感谢观看|谢谢观看)", normalized):
            emit(start, end, "non_factual", reason="non_factual_expression")
            return
        parsed = parse_clause(raw, aliases)
        if parsed:
            emit(start, end, "fact", parsed)
            return
        if not raw.strip():
            emit(start, end, "non_factual", reason="structural_whitespace")
            return
        match = re.search(r"[，,；;。！？!?\n]+", raw) or re.search(r"同时|并且|而且|但是|但|且", raw)
        if match:
            a, b = start + match.start(), start + match.end()
            if start < a:
                parse_span(start, a)
            emit(a, b, "non_factual", reason="structural_separator")
            if b < end:
                parse_span(b, end)
        else:
            emit(start, end, "unresolved", reason="outside_closed_grammar")
    for match in re.finditer(r"[^。！？!?；;\n]+[。！？!?；;\n]*|[。！？!?；;\n]+", text):
        parse_span(match.start(), match.end())
    return units


def _segment_id(contract: dict, start: int, end: int) -> str:
    for segment in contract["segments"]:
        if segment["start"] <= start < end <= segment["end"]:
            return segment["id"]
    return contract["segments"][0]["id"] if contract["segments"] else "s1"


def automatic_plan(contract: dict, product_id: str, applicable_versions: str | list[str], aliases: tuple[str, ...] = ()) -> dict:
    compiled = compile_text(contract["input_text"], aliases)
    versions = [applicable_versions] if isinstance(applicable_versions, str) else list(applicable_versions)
    versions = [str(value) for value in versions if str(value)] or ["unresolved_scope"]
    units: list[dict] = []
    atoms: list[dict] = []
    for ui, source in enumerate(compiled, 1):
        uid = f"u{ui}"
        atom_ids: list[str] = []
        facts = list(source["facts"])
        # Unknown factual insurance statements are kept as an atom so a host
        # semantic result can later complete them. Pure headings/prose are not.
        if product_id and source["kind"] == "unresolved" and re.search(r"保险|保单|合同|产品|条款|贷款|红利|分红|投保|领取|给付|赔付|允许|不得|保证", source["quote"]):
            facts = [_fact("unclassified_statement", None, "none")]
        kind = "personal" if source["kind"] == "personal" else ("fact" if facts else ("non_factual" if source["kind"] != "unresolved" else "unresolved"))
        for fact in facts:
            fact_class = "personal" if source["kind"] == "personal" else "product"
            atom_versions = [""] if fact_class == "personal" else versions
            for applicable_version in atom_versions:
                aid = f"a{len(atoms)+1}"
                atom_ids.append(aid)
                atom = {"id": aid, "unit_ids": [uid], "fact_class": fact_class, **fact}
                if fact_class == "product":
                    # A missing product is a scope decision for the host/user,
                    # not a malformed automatically generated plan. Never bind
                    # this placeholder to any product evidence.
                    atom.update({"product_id": product_id or "unresolved_product_scope", "applicable_version": applicable_version})
                atoms.append(atom)
        unit = {"id": uid, "segment_id": _segment_id(contract, source["start"], source["end"]),
                "start": source["start"], "end": source["end"], "quote": source["quote"], "kind": kind, "atom_ids": atom_ids}
        if kind in {"non_factual", "unresolved"}:
            unit["reason"] = source["reason"] or "outside_closed_grammar"
        units.append(unit)
    audit = []
    for segment in contract["segments"]:
        audit.append({"segment_id": segment["id"], "atom_ids": [a["id"] for a in atoms if any(u["segment_id"] == segment["id"] for u in units if u["id"] in a["unit_ids"])], "reviewed_full_segment": True, "omissions": []})
    return {"schema": "personal-kb.review-plan.v1", "input_id": contract["input_id"], "snapshot_id": contract["snapshot_id"], "units": units, "atoms": atoms, "coverage_audit": audit}


def semantic_coverage_errors(contract: dict, plan: dict, expected_plan: dict) -> list[str]:
    submitted = [a for a in plan.get("atoms", []) if a.get("fact_class") == "product"]
    expected_atoms = [a for a in expected_plan.get("atoms", []) if a.get("fact_class") == "product"]
    submitted_known = [a for a in submitted if a.get("property") != "unclassified_statement"]
    expected_known = [a for a in expected_atoms if a.get("property") != "unclassified_statement"]
    submitted_unknown = [a for a in submitted if a.get("property") == "unclassified_statement"]
    expected_unknown = [a for a in expected_atoms if a.get("property") == "unclassified_statement"]
    keys = ("product_id", "applicable_version", "property", "operator", "polarity", "value", "unit", "conditions")
    expected = Counter(canonical({k: a.get(k) for k in keys}) for a in expected_known)
    got = Counter(canonical({k: a.get(k) for k in keys}) for a in submitted_known)
    if len(expected_known) != len(submitted_known):
        return ["semantic_coverage_mismatch"]
    if expected != got:
        return ["claim_normalization_mismatch"]
    units = {u.get("id"): u for u in plan.get("units", []) if isinstance(u, dict)}
    expected_units = {u.get("id"): u for u in expected_plan.get("units", []) if isinstance(u, dict)}

    def atom_intervals(atom: dict, unit_map: dict) -> list[tuple[int, int]]:
        return sorted(
            (int(unit_map[uid]["start"]), int(unit_map[uid]["end"]))
            for uid in atom.get("unit_ids", [])
            if uid in unit_map
        )

    def interval_covered(start: int, end: int, intervals: list[tuple[int, int]]) -> bool:
        cursor = start
        for left, right in sorted((max(start, left), min(end, right)) for left, right in intervals if left < end and right > start):
            if left > cursor:
                return False
            cursor = max(cursor, right)
            if cursor >= end:
                return True
        return cursor >= end

    expected_bindings: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    for atom in expected_known:
        spans = tuple(atom_intervals(atom, expected_units))
        if spans:
            expected_bindings.append((canonical({k: atom.get(k) for k in keys}), spans))
    submitted_bindings: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    for atom in submitted_known:
        spans = tuple(atom_intervals(atom, units))
        if spans:
            submitted_bindings.append((canonical({k: atom.get(k) for k in keys}), spans))
    available = list(range(len(submitted_bindings)))
    # Hosts may bind several propositions to one larger raw unit, but the unit
    # must contain the exact backend-derived occurrence.  This permits legal
    # alternate splitting while still rejecting swaps across occurrences.
    for signature, spans in sorted(expected_bindings, key=lambda row: (sum(end - start for start, end in row[1]), row[1])):
        matched = next((index for index in available
                        if submitted_bindings[index][0] == signature
                        and all(interval_covered(start, end, list(submitted_bindings[index][1])) for start, end in spans)), None)
        if matched is None:
            return ["claim_normalization_mismatch"]
        available.remove(matched)
    if available:
        return ["claim_normalization_mismatch"]

    # Unknown atoms represent raw regions, not a backend-known proposition
    # count. A host may refine one unknown region into several pending atoms,
    # provided it keeps the same product/scope and covers that region without
    # replacing any deterministic obligation.
    expected_regions: list[tuple[str, str, int, int]] = []
    allowed_regions: list[tuple[str, str, int, int]] = []
    for atom in expected_unknown:
        spans = atom_intervals(atom, expected_units)
        if spans:
            product_id, version = str(atom.get("product_id")), str(atom.get("applicable_version"))
            expected_regions.extend((product_id, version, start, end) for start, end in spans)
            allowed_regions.extend((product_id, version, start, end) for start, end in spans)
            segment_ids = {expected_units[uid].get("segment_id") for uid in atom.get("unit_ids", []) if uid in expected_units}
            allowed_regions.extend(
                (product_id, version, int(unit["start"]), int(unit["end"]))
                for unit in expected_units.values()
                if unit.get("segment_id") in segment_ids
                and unit.get("kind") == "non_factual"
                and unit.get("reason") == "structural_separator"
            )
    submitted_regions: list[tuple[str, str, int, int]] = []
    for atom in submitted_unknown:
        submitted_regions.extend(
            (str(atom.get("product_id")), str(atom.get("applicable_version")), start, end)
            for start, end in atom_intervals(atom, units)
        )
    if bool(expected_regions) != bool(submitted_regions):
        return ["semantic_coverage_mismatch"]

    for product_id, version, start, end in submitted_regions:
        allowed = [(es, ee) for ep, ev, es, ee in allowed_regions if ep == product_id and ev == version]
        if not interval_covered(start, end, allowed):
            return ["semantic_coverage_mismatch"]
    for product_id, version, start, end in expected_regions:
        supplied = [(ss, se) for sp, sv, ss, se in submitted_regions if sp == product_id and sv == version]
        if not interval_covered(start, end, supplied):
            return ["semantic_coverage_mismatch"]
    return []


def _eligible(source: dict, atom: dict) -> bool:
    return (atom.get("fact_class") == "product"
            and source.get("资料类型") == "产品资料"
            and source.get("qualification") == "evidence"
            and source.get("状态") in {"当前", "有效", "生效"}
            and source.get("product_id") == atom.get("product_id")
            and source.get("applicable_version") == atom.get("applicable_version"))


def _relevant_properties(quote: str) -> list[str]:
    """Conservatively retain unresolved source spans that may affect a closed fact."""
    text = _normalize(quote)
    if re.search(r"(?:不约定|未约定|不涉及|未涉及|不包含|未说明|不说明)(?:具体)?", text):
        return []
    properties: list[str] = []
    if re.search(r"(?:贷款|借款).{0,24}(?:期限|个月)|(?:期限|个月).{0,24}(?:贷款|借款)", text):
        properties.append("loan_term_months")
    if re.search(r"(?:贷款|借款).{0,24}(?:金额|上限|%|百分)|(?:金额|上限|%|百分).{0,24}(?:贷款|借款)", text) \
            or re.search(r"现金价值.{0,16}(?:比例|%|百分|不得超过|不超过)|(?:比例|%|百分|不得超过|不超过).{0,16}现金价值", text):
        properties.append("loan_limit_percent")
    if re.search(r"(?:贷款|借款).{0,28}(?:中止|终止|效力)|(?:中止|终止|效力).{0,28}(?:贷款|借款)", text):
        properties.append("loan_termination_condition")
    if re.search(r"(?:红利|分红).{0,16}(?:保证|不保证|零|固定|承诺|可能)|(?:保证|不保证|零|固定|承诺|可能).{0,16}(?:红利|分红)", text):
        properties.extend(["dividend_guaranteed", "dividend_zero_possible"])
    return list(dict.fromkeys(properties))


def _explicitly_irrelevant(quote: str) -> bool:
    return bool(re.search(r"(?:不约定|未约定|不涉及|未涉及|不包含|未说明|不说明)(?:具体)?", _normalize(quote)))


def _semantic_topics(quote: str) -> list[str]:
    text = _normalize(quote)
    topics = []
    if re.search(r"贷款|借款|现金价值", text):
        topics.append("loan")
    if re.search(r"红利|分红", text):
        topics.append("dividend")
    if re.search(r"投保年龄|被保险人年龄|周岁", text):
        topics.append("insured_age")
    return topics


def _property_topic(property_name: str) -> str:
    if property_name.startswith("dividend"):
        return "dividend"
    if property_name == "insured_age_years":
        return "insured_age"
    if property_name.startswith("loan"):
        return "loan"
    return ""


def _semantic_facets(quote: str) -> set[str]:
    """Return conservative domain concepts without pretending to parse a fact."""
    text = _normalize(quote)
    facets: set[str] = set()
    if re.search(r"提前(?:偿还|还款)|(?:偿还|还款).{0,4}(?:贷款|借款)", text):
        facets.add("loan:repayment")
    if re.search(r"(?:线上|在线|网络).{0,8}(?:提交|申请)|(?:提交|申请).{0,8}(?:线上|在线|网络)", text):
        facets.add("process:online_submission")
    if (re.search(r"现金价值.{0,12}(?:贷款|借款)|(?:申请|允许).{0,8}(?:贷款|借款)", text)
            and "loan:repayment" not in facets):
        facets.add("loan:borrowing")
    if re.search(r"贷款期限|借款期限|期限.{0,8}(?:贷款|借款)|(?:贷款|借款).{0,8}期限", text):
        facets.add("loan:term")
    if re.search(r"(?:贷款|借款).{0,12}(?:金额|比例|上限)|现金价值.{0,10}(?:%|百分|比例|上限)", text):
        facets.add("loan:limit")
    if re.search(r"(?:贷款|借款).{0,16}(?:效力|中止|终止)|(?:效力|中止|终止).{0,16}(?:贷款|借款)", text):
        facets.add("loan:termination")
    if re.search(r"红利|分红", text):
        facets.add("dividend")
    if re.search(r"投保年龄|被保险人年龄|周岁", text):
        facets.add("insured_age")
    return facets


def _same_unknown_context(source_text: str, atom_text: str, source_properties: set[str], atom_properties: set[str],
                          source_facets: set[str], atom_facets: set[str]) -> bool:
    if source_properties.intersection(atom_properties) or source_facets.intersection(atom_facets):
        return True
    source_normalized, atom_normalized = _normalize(source_text), _normalize(atom_text)
    return (len(source_normalized) >= 4 and source_normalized in atom_normalized) \
        or (len(atom_normalized) >= 4 and atom_normalized in source_normalized)


def build_scope(sources: list[dict], resolution: dict, plan: dict) -> dict:
    rows = []
    source_inventory = []
    unit_map = {u.get("id"): u for u in plan.get("units", []) if isinstance(u, dict)}
    plan_atoms = []
    for atom in plan.get("atoms", []):
        if atom.get("fact_class") != "product":
            continue
        atom_units = [unit_map[uid] for uid in atom.get("unit_ids", []) if uid in unit_map]
        quote = "".join(unit.get("quote", "") for unit in sorted(atom_units, key=lambda unit: unit.get("start", 0)))
        impact = set(_relevant_properties(quote))
        if atom.get("property") != "unclassified_statement":
            impact.add(str(atom.get("property")))
        topics = set(_semantic_topics(quote))
        property_topic = _property_topic(str(atom.get("property") or ""))
        if property_topic:
            topics.add(property_topic)
        plan_atoms.append({"atom": atom, "text": quote, "impact_properties": impact, "topics": topics,
                           "facets": _semantic_facets(quote)})
    for source in sources:
        if source.get("资料类型") != "产品资料" or source.get("qualification") != "evidence" or source.get("状态") not in {"当前", "有效", "生效"}:
            continue
        if resolution.get("status") != "resolved" or source.get("product_id") != resolution.get("product_id"):
            continue
        if resolution.get("product_year") and source.get("product_year") and source.get("product_year") != resolution.get("product_year"):
            continue
        if resolution.get("applicable_versions") and source.get("applicable_version") not in resolution.get("applicable_versions", []):
            continue
        revision_id = str(source.get("revision_id") or "rev-" + str(source.get("sha256")))
        rows.append({"revision_id": revision_id, "document_id": source.get("document_id"), "source_relative": source.get("source_relative"), "sha256": source.get("sha256"),
                     "product_id": source.get("product_id"), "applicable_version": source.get("applicable_version"), "status": "current", "qualification": "evidence", "integrity_verified": True, "allowed_now": True})
        for chunk in source.get("chunks", []):
            compiled = compile_text(str(chunk.get("text", "")))
            for index, unit in enumerate(compiled):
                deterministic_properties = [str(fact.get("property")) for fact in unit.get("facts", [])
                                            if fact.get("property") and fact.get("property") != "unclassified_statement"]
                mixed_unknown = any(fact.get("property") == "unclassified_statement" for fact in unit.get("facts", []))
                if unit["kind"] != "unresolved" and not mixed_unknown:
                    continue
                properties = _relevant_properties(unit["quote"])
                properties.extend(deterministic_properties)
                normalized = _normalize(unit["quote"])
                if unit["kind"] == "unresolved" and re.match(r"^(?:若|如果|当|除非|仅当|在.+时|(?:已|未).+时)", normalized):
                    # A detached condition/exception still constrains the fact
                    # that follows it in the same sentence, even when the
                    # closed grammar cannot normalize the condition itself.
                    for later in compiled[index + 1:index + 4]:
                        if later["kind"] == "fact":
                            properties.extend(str(fact.get("property")) for fact in later["facts"] if fact.get("property"))
                            break
                        if re.search(r"[。；;！？!?\n]", later["quote"]):
                            break
                properties = list(dict.fromkeys(properties))
                location = {"document_id": source.get("document_id"), "revision_id": revision_id, "chunk_id": chunk.get("chunk_id"),
                            "start": unit["start"], "end": unit["end"], "quote": unit["quote"], "properties": properties,
                            "topics": _semantic_topics(unit["quote"]), "facets": sorted(_semantic_facets(unit["quote"])),
                            "kind": "mixed_unknown" if mixed_unknown else "unresolved",
                            "explicitly_irrelevant": _explicitly_irrelevant(unit["quote"]),
                            "product_id": source.get("product_id"), "applicable_version": source.get("applicable_version")}
                source_inventory.append(location)
    unclassified_locations = []
    for location in source_inventory:
        if location["explicitly_irrelevant"]:
            continue
        eligible = [item for item in plan_atoms
                    if str(item["atom"].get("product_id")) == str(location.get("product_id"))
                    and str(item["atom"].get("applicable_version")) == str(location.get("applicable_version"))]
        location_properties, location_topics, location_facets = set(location["properties"]), set(location["topics"]), set(location["facets"])
        unknown_matches = [item for item in eligible
                           if item["atom"].get("property") == "unclassified_statement"
                           and _same_unknown_context(location["quote"], item["text"], location_properties,
                                                     item["impact_properties"], location_facets, item["facets"])]
        known_matches = [item for item in eligible
                         if item["atom"].get("property") != "unclassified_statement"
                         and (str(item["atom"].get("property")) in location_properties
                              or (not location_properties and location_topics.intersection(item["topics"])))]
        if location["kind"] == "mixed_unknown":
            matches = unknown_matches
        else:
            matches = list({item["atom"]["id"]: item for item in [*known_matches, *unknown_matches]}.values())
        for item in matches:
            unclassified_locations.append({**location, "atom_id": item["atom"]["id"]})
    ids = [x["revision_id"] for x in rows]
    return {"schema": "personal-kb.retrieval-scope.v1", "eligible_revisions": rows, "eligible_revision_ids": ids,
            "inspected_revision_ids": list(ids), "unread_revision_ids": [], "scope_bound": resolution.get("status") == "resolved",
            "all_eligible_revisions_inspected": True, "extraction_complete": True, "source_unclassified_inventory": source_inventory,
            "unclassified_relevant_spans": len(unclassified_locations), "unclassified_relevant_locations": unclassified_locations, "next_cursor": None}


def _source_candidates(sources: list[dict], atom: dict) -> list[dict]:
    result = []
    for source in sources:
        if not _eligible(source, atom):
            continue
        revision_id = str(source.get("revision_id") or "rev-" + str(source.get("sha256")))
        for chunk in source.get("chunks", []):
            text = str(chunk.get("text", ""))
            for unit in compile_text(text):
                for fact in unit["facts"]:
                    if fact.get("property") == atom.get("property"):
                        result.append({"source": source, "revision_id": revision_id, "chunk": chunk, "text": text, "span": unit, "fact": fact})
    return result


def _citation(candidate: dict) -> dict:
    source, chunk, span = candidate["source"], candidate["chunk"], candidate["span"]
    return {"file": source.get("source_relative"), "source_relative": source.get("source_relative"), "document_id": source.get("document_id"),
            "revision_id": candidate["revision_id"], "sha256": source.get("sha256"), "document_version": source.get("document_version") or source.get("版本"),
            "applicable_version": source.get("applicable_version"), "evidence_level": source.get("证据级别"), "chunk_id": chunk.get("chunk_id"),
            "start": span["start"], "end": span["end"], "quote": span["quote"], "text": candidate["text"][:1800],
            "source_value": candidate["fact"].get("value"), "source_conditions": candidate["fact"].get("conditions", {})}


def _verdict(atom: dict, candidates: list[dict], scope: dict) -> tuple[str, str, list[dict]]:
    if atom.get("fact_class") == "personal":
        return "needs_person_confirmation", "个人经历或服务做法需本人确认，不能由产品资料代为证明。", []
    if atom.get("property") == "unclassified_statement":
        return "needs_semantic_review", "该事实超出确定性比较域，需基于合格原文完成语义核验。", []
    unresolved = [location for location in scope.get("unclassified_relevant_locations", [])
                  if location.get("atom_id") == atom.get("id")]
    if not candidates:
        if unresolved:
            return "needs_semantic_review", "当前合格资料含可能影响此属性的未解释原文，需完成语义核验。", []
        exhausted = scope.get("scope_bound") and scope.get("all_eligible_revisions_inspected") and scope.get("extraction_complete") and scope.get("unread_revision_ids") == []
        return ("not_covered" if exhausted else "retrieval_incomplete"), "当前合格资料未覆盖此属性。", []
    keys = ("value", "unit", "operator", "polarity", "conditions")
    signatures = {canonical({k: c["fact"].get(k) for k in keys}) for c in candidates}
    citations = [_citation(c) for c in candidates]
    if len(signatures) > 1:
        if unresolved:
            return "needs_semantic_review", "当前资料包含尚未归一化的条件或例外，不能把不同语境直接判为冲突。", []
        return "conflict", "当前合格资料对同一属性存在冲突。", citations
    source = candidates[0]["fact"]
    for key in ("operator", "polarity", "value", "unit"):
        if atom.get(key) != source.get(key):
            return "contradicted", f"待审事实的{key}与当前资料不同。", citations
    claimed, required = atom.get("conditions", {}), source.get("conditions", {})
    if any(key in claimed and claimed[key] != value for key, value in required.items()):
        return "contradicted", "待审事实的适用条件与当前资料相反。", citations
    missing = [key for key in required if key not in claimed]
    if missing:
        labels={"valuation_time":"申请时的计价时点", "calculation_basis":"扣除各项欠款及利息后的计算基数", "per_loan":"每次贷款", "loan_principal_interest":"贷款本金及利息", "cash_value_threshold":"达到现金价值", "other_unpaid_items":"其他未还款项"}
        return "missing_condition", "遗漏关键条件：" + "、".join(labels.get(key,key) for key in missing), citations
    if any(key not in required for key in claimed):
        return "needs_semantic_review", "待审事实增加了资料未能确定的条件。", []
    if unresolved:
        return "needs_semantic_review", "当前合格资料含可能影响此属性的未解释原文，需完成语义核验。", []
    return "supported", "", citations


def _find_units(plan: dict, atom: dict) -> list[dict]:
    unit_ids = set(atom.get("unit_ids", []))
    return sorted((u for u in plan.get("units", []) if u.get("id") in unit_ids), key=lambda unit: unit.get("start", 0))


def _validate_semantic_results(value: Any, contract: dict, plan_id: str, checks: list[dict], sources: list[dict]) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or value.get("schema") != "personal-kb.semantic-results.v1":
        return ["semantic_results_schema"]
    if value.get("input_id") != contract["input_id"]:
        errors.append("stale_input_id")
    if value.get("snapshot_id") != contract["snapshot_id"]:
        errors.append("stale_snapshot_id")
    if value.get("plan_id") != plan_id:
        errors.append("stale_plan_id")
    rows = value.get("results")
    if not isinstance(rows, list):
        return errors + ["semantic_results_array_required"]
    row_ids = [row.get("atom_id") for row in rows if isinstance(row, dict)]
    if len(row_ids) != len(rows) or len(set(row_ids)) != len(row_ids):
        errors.append("semantic_result_ids")
    check_map = {c["atom_id"]: c for c in checks}
    source_map = {(str(s.get("document_id")), str(s.get("revision_id") or "rev-" + str(s.get("sha256")))): s for s in sources}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"atom_id", "verdict", "reason", "citations"}:
            errors.append("semantic_result_shape")
            continue
        check = check_map.get(row.get("atom_id"))
        if check is None:
            errors.append("unknown_atom_id")
            continue
        if check["verification_status"] != "needs_semantic_review":
            errors.append("deterministic_override_forbidden" if check["verification_method"] == "deterministic_property_compare" else "semantic_result_not_pending")
            continue
        if row.get("verdict") not in {"supported", "contradicted", "needs_semantic_review"} or not str(row.get("reason") or "").strip() or not isinstance(row.get("citations"), list):
            errors.append("semantic_result_shape")
            continue
        if row.get("verdict") in {"supported", "contradicted"} and not row["citations"]:
            errors.append("citation_required")
            continue
        for citation in row["citations"]:
            if not isinstance(citation, dict) or set(citation) != {"document_id", "revision_id", "chunk_id", "start", "end", "quote"}:
                errors.append("citation_shape")
                continue
            source = source_map.get((str(citation.get("document_id")), str(citation.get("revision_id"))))
            atom = next((a for a in check.get("_atoms", []) if a.get("id") == row.get("atom_id")), None)
            if source is None or atom is None or not _eligible(source, atom):
                errors.append("source_not_eligible")
                continue
            chunk = next((x for x in source.get("chunks", []) if x.get("chunk_id") == citation.get("chunk_id")), None)
            start, end = citation.get("start"), citation.get("end")
            if chunk is None or type(start) is not int or type(end) is not int or not (0 <= start < end <= len(str(chunk.get("text", "")))) or str(chunk.get("text", ""))[start:end] != citation.get("quote"):
                errors.append("citation_mismatch")
    return list(dict.fromkeys(errors))


def execute_review(query: str, sources: list[dict], resolution: dict, snapshot_id: str, host_plan: dict | None = None, semantic_results: dict | None = None) -> dict:
    contract = prepare_contract(query, snapshot_id)
    product_id = str(resolution.get("product_id") or "")
    applicable = [str(value) for value in resolution.get("applicable_versions", []) if str(value)]
    source_aliases: list[str] = []
    for source in sources:
        if source.get("product_id") == product_id:
            source_aliases.extend(str(value) for value in [source.get("product_name"), source.get("业务对象"), *(source.get("product_aliases") or [])] if str(value or ""))
    aliases = tuple(dict.fromkeys(str(value) for value in [resolution.get("product_name"), *(resolution.get("matched_aliases") or []), *source_aliases] if str(value or "")))
    expected_plan = automatic_plan(contract, product_id, applicable, aliases)
    plan = host_plan if host_plan is not None else expected_plan
    validation = validate_plan(contract, plan)
    if validation["valid"] and host_plan is not None:
        validation["errors"].extend(semantic_coverage_errors(contract, plan, expected_plan))
        validation["valid"] = not validation["errors"]
        validation["structural_complete"] = validation["valid"]
    validation["plan_id"] = digest(plan)
    if not validation["valid"]:
        return {"status": "invalid_review_plan", "review_contract": contract, "review_validation": validation,
                "retrieval_scope": None, "claim_checks": [], "review_summary": {"complete": False, "all_supported": False}}
    scope = build_scope(sources, resolution, plan)
    checks: list[dict] = []
    for atom in plan.get("atoms", []):
        atom_units = _find_units(plan, atom)
        unit = atom_units[0] if atom_units else {}
        atom_quote = "".join(str(value.get("quote") or "") for value in atom_units)
        atom_intervals = [{"unit_id": value.get("id"), "start": value.get("start"), "end": value.get("end"), "quote": value.get("quote")} for value in atom_units]
        candidates = _source_candidates(sources, atom)
        status, difference, evidence = _verdict(atom, candidates, scope)
        method = "host_agent_semantic_review" if atom.get("property") in {"unclassified_statement", "personal_experience"} else "deterministic_property_compare"
        fact_class = "个人经历" if atom.get("fact_class") == "personal" else "产品事实"
        checks.append({"atom_id": atom["id"], "parent_id": unit.get("segment_id"), "parent_text": atom_quote, "claim": atom_quote,
                       "fact_class": fact_class, "topic": "红利" if str(atom.get("property", "")).startswith("dividend") else "保单贷款",
                       "property": atom.get("property"), "claimed_value": atom.get("value"), "claimed_conditions": atom.get("conditions", {}),
                       "source_start": min((value.get("start") for value in atom_units), default=None),
                       "source_end": max((value.get("end") for value in atom_units), default=None), "source_intervals": atom_intervals,
                       "comparison": atom.get("operator"), "polarity": atom.get("polarity"),
                       "verification_method": method, "verification_status": status, "status": STATUS_LABELS[status], "difference": difference,
                       "evidence": evidence, "_atoms": [atom]})
    if semantic_results is not None:
        errors = _validate_semantic_results(semantic_results, contract, validation["plan_id"], checks, sources)
        if errors:
            validation["errors"] = errors
            return {"status": "invalid_semantic_result", "review_contract": contract, "review_validation": validation,
                    "retrieval_scope": scope, "claim_checks": [{k: v for k, v in c.items() if k != "_atoms"} for c in checks],
                    "review_summary": {"complete": False, "all_supported": False}}
        by_id = {row["atom_id"]: row for row in semantic_results["results"]}
        resolved_locations: set[tuple[str, str, int, int, str]] = set()
        for check in checks:
            row = by_id.get(check["atom_id"])
            if row:
                check["verification_status"] = row["verdict"]
                check["status"] = STATUS_LABELS[row["verdict"]]
                check["difference"] = row["reason"]
                check["verification_method"] = "host_agent_semantic_review"
                check["evidence"] = []
                for citation in row["citations"]:
                    source = next(s for s in sources if str(s.get("document_id")) == citation["document_id"] and str(s.get("revision_id") or "rev-" + str(s.get("sha256"))) == citation["revision_id"])
                    check["evidence"].append({"file": source.get("source_relative"), "source_relative": source.get("source_relative"), "document_id": citation["document_id"],
                                              "revision_id": citation["revision_id"], "sha256": source.get("sha256"), "chunk_id": citation["chunk_id"],
                                              "start": citation["start"], "end": citation["end"], "quote": citation["quote"], "text": citation["quote"]})
                    atom = check.get("_atoms", [{}])[0]
                    for location in scope.get("unclassified_relevant_locations", []):
                        if (location.get("revision_id") == citation["revision_id"] and location.get("chunk_id") == citation["chunk_id"]
                                and location.get("atom_id") == atom.get("id")
                                and location.get("applicable_version") == atom.get("applicable_version")
                                and citation["start"] <= location.get("start", -1) and citation["end"] >= location.get("end", -1)):
                            resolved_locations.add((str(location.get("revision_id")), str(location.get("chunk_id")), int(location.get("start")), int(location.get("end")), str(location.get("atom_id"))))
        if resolved_locations:
            scope["unclassified_relevant_locations"] = [location for location in scope.get("unclassified_relevant_locations", [])
                                                        if (str(location.get("revision_id")), str(location.get("chunk_id")), int(location.get("start")), int(location.get("end")), str(location.get("atom_id"))) not in resolved_locations]
            scope["unclassified_relevant_spans"] = len(scope["unclassified_relevant_locations"])
    for unit in plan.get("units", []):
        if unit.get("kind") == "non_factual" and unit.get("reason") == "non_factual_expression":
            checks.append({"atom_id": "classification:" + unit["id"], "parent_id": unit.get("segment_id"), "parent_text": unit.get("quote"), "claim": unit.get("quote"),
                           "fact_class": "非事实表达", "topic": "非事实表达", "property": "non_factual_expression", "claimed_value": None,
                           "claimed_conditions": {}, "source_start": unit.get("start"), "source_end": unit.get("end"), "comparison": "equals", "polarity": "affirmative",
                           "verification_method": "input_classification", "verification_status": "supported", "status": STATUS_LABELS["supported"], "difference": "", "evidence": [],
                           "_classification_only": True})
    pending = {"retrieval_incomplete", "needs_semantic_review", "needs_person_confirmation"}
    terminal = {"supported", "contradicted", "missing_condition", "not_covered", "conflict"}
    fact_checks = [c for c in checks if not c.get("_classification_only")]
    unresolved_units = list(validation.get("unresolved_unit_ids") or [])
    retrieval_complete = (scope.get("scope_bound") is True and scope.get("all_eligible_revisions_inspected") is True
                          and scope.get("extraction_complete") is True and scope.get("unclassified_relevant_spans") == 0
                          and scope.get("unread_revision_ids") == [])
    complete = (bool(fact_checks) and not unresolved_units and retrieval_complete
                and all(c["verification_status"] in terminal for c in fact_checks))
    public_checks = [{k: v for k, v in c.items() if k not in {"_atoms", "_classification_only"}} for c in checks]
    summary = {"submitted_parent_count": len(contract["segments"]), "atomic_fact_count": len(fact_checks), "reviewed_atomic_count": len(fact_checks),
               "unresolved_atomic_count": sum(c["verification_status"] in pending for c in fact_checks) + len(unresolved_units),
               "unresolved_atom_ids": [c["atom_id"] for c in fact_checks if c["verification_status"] in pending] + ["unit:" + uid for uid in unresolved_units],
               "failed_atom_ids": [c["atom_id"] for c in checks if c["verification_status"] in {"contradicted", "missing_condition", "conflict"}],
               "input_id": contract["input_id"], "complete": complete, "all_supported": complete and all(c["verification_status"] == "supported" for c in fact_checks)}
    return {"status": "ready", "review_contract": contract, "review_plan": plan, "review_validation": validation, "retrieval_scope": scope,
            "claim_checks": public_checks, "review_summary": summary}
