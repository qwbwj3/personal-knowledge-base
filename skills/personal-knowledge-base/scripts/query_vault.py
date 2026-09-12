#!/usr/bin/env python3
"""Read-only, scope-checked wrapper around claude-obsidian retrieval."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path


def die(message: str, code: int = 2) -> None:
    print(json.dumps({"schema": "llm-wiki.query-error.v1", "error": message}, ensure_ascii=False))
    raise SystemExit(code)


def resolved(path: str, *, must_exist: bool = True) -> Path:
    return Path(path).expanduser().resolve(strict=must_exist)


def inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def asks_for_history(value: str) -> bool:
    """Return whether a query explicitly needs superseded source versions."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    markers = (
        "历史版本",
        "旧版",
        "老版本",
        "上一版",
        "此前版本",
        "更新前",
        "变更前",
        "新旧对比",
        "版本对比",
        "版本区别",
        "superseded",
        "previous version",
        "old version",
    )
    return any(marker in normalized for marker in markers)


def asks_for_navigation(value: str) -> bool:
    """Return whether generated navigation/index pages are themselves relevant."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    markers = (
        "来源导航",
        "资料导航",
        "知识库索引",
        "全库索引",
        "知识图谱",
        "图谱节点",
        "主题节点",
        "vault index",
        "source navigation",
    )
    return any(marker in normalized for marker in markers)


def superseded_source_pages(vault: Path) -> set[str]:
    """Read canonical source status and return pages belonging to old versions."""

    ledger_path = vault / "wiki/meta/ledgers/source-ledger.json"
    if not ledger_path.is_file():
        return set()
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"cannot read canonical source ledger: {exc}")
    if ledger.get("schema") != "claude-obsidian.source-ledger.v1" or not isinstance(ledger.get("sources"), dict):
        die("canonical source ledger has an unsupported shape")
    pages: set[str] = set()
    for record in ledger["sources"].values():
        if not isinstance(record, dict) or record.get("review_status") != "superseded":
            continue
        for page in record.get("pages", []):
            if isinstance(page, str) and page.startswith("wiki/sources/"):
                pages.add(page)
    return pages


def active_source_pages(vault: Path) -> set[str]:
    """Return current canonical source pages eligible for evidence promotion."""

    ledger_path = vault / "wiki/meta/ledgers/source-ledger.json"
    if not ledger_path.is_file():
        return set()
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"cannot read canonical source ledger: {exc}")
    if ledger.get("schema") != "claude-obsidian.source-ledger.v1" or not isinstance(ledger.get("sources"), dict):
        die("canonical source ledger has an unsupported shape")
    pages: set[str] = set()
    for record in ledger["sources"].values():
        if not isinstance(record, dict) or record.get("review_status") != "active":
            continue
        for page in record.get("pages", []):
            if isinstance(page, str) and page.startswith("wiki/sources/") and not page.endswith("来源导航.md"):
                pages.add(page)
    return pages


def graph_evidence_pages(vault: Path, relative: str, allowed: set[str]) -> list[str]:
    """Read validated graph navigation links and return only current source pages."""

    graph_root = (vault / "wiki/知识图谱").resolve()
    page = (vault / relative).resolve(strict=True)
    if not inside(page, [graph_root]) or not page.is_file():
        die("graph candidate escaped the Chinese graph root")
    data = page.read_bytes()
    if len(data) > 128 * 1024:
        die(f"graph page is too large: {relative}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        die(f"graph page is not UTF-8: {relative}")
    promoted = []
    for match in re.finditer(r"`(wiki/sources/[^`\n]+\.md)`", text):
        source = match.group(1)
        if source in allowed and source not in promoted:
            promoted.append(source)
    return promoted


def address_from_page(data: bytes) -> str | None:
    """Read an existing canonical page address from UTF-8 frontmatter."""

    head = data[:4096].decode("utf-8", errors="ignore")
    match = re.search(r"(?m)^address:\s*([^\s]+)\s*$", head)
    return match.group(1) if match else None


def allocate_page_budgets(total: int, count: int) -> list[int]:
    """Give every result evidence while reserving most bytes for top-ranked pages."""

    if count < 1:
        return []
    minimum = 1024
    base = [minimum] * count
    remaining = total - minimum * count
    if remaining < 0:
        raise ValueError("not enough bytes for selected pages")
    # Retrieval order is already relevance-ranked. A bounded 8:4:2:1... bonus
    # keeps several sources visible but prevents weak tail hits from starving the
    # strongest evidence page, which equal division did for long course notes.
    weights = [8, 4, 2] + [1] * max(0, count - 3)
    weights = weights[:count]
    weight_total = sum(weights)
    used = 0
    for index, weight in enumerate(weights):
        bonus = remaining * weight // weight_total
        base[index] += bonus
        used += bonus
    for index in range(remaining - used):
        base[index % count] += 1
    return base


def decode_prefix(data: bytes, limit: int) -> tuple[str, int]:
    """Decode at most ``limit`` bytes without returning a partial code point."""

    included = data[: max(0, limit)]
    text = included.decode("utf-8", errors="ignore")
    encoded = text.encode("utf-8")
    return text, len(encoded)


def decode_suffix(data: bytes, limit: int) -> tuple[str, int, int]:
    """Decode a UTF-8-safe suffix and return text, start byte, and byte count."""

    if limit <= 0:
        return "", len(data), 0
    start = max(0, len(data) - limit)
    while start < len(data) and data[start] & 0xC0 == 0x80:
        start += 1
    text = data[start:].decode("utf-8", errors="ignore")
    included = len(text.encode("utf-8"))
    return text, start, included


def query_terms(value: str) -> list[tuple[str, int]]:
    """Build weighted literal terms, including CJK n-grams for long questions."""

    normalized = unicodedata.normalize("NFKC", value)
    weighted: dict[str, int] = {}

    def add(term: str, weight: int) -> None:
        term = term.strip().casefold()
        if len(term) < 2 or len(term.encode("utf-8")) > 160:
            return
        weighted[term] = max(weighted.get(term, 0), weight)

    for match in re.finditer(r'[“”"\'「『](.*?)[“”"\'」』]', normalized):
        term = match.group(1).strip()
        add(term, 10_000 + len(term) ** 2)

    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{1,79}", normalized):
        # A year or claimed percentage in an insurance question should not
        # outrank the actual responsibility/condition phrase.  The generic
        # base adapter gave ASCII-like tokens a high weight, which made “2026”
        # pull the evidence window back to a PDF cover page.
        add(token, (25 if token.isdigit() else 500) + len(token) ** 2)

    cjk_runs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,120}", normalized)
    for run in cjk_runs:
        if len(run) <= 24:
            add(run, 400 + len(run) ** 2)
        # Character n-grams let a natural-language question locate a short
        # phrase in a long Chinese page without requiring a tokenizer.
        for width in (8, 6, 5, 4, 3, 2):
            if len(run) < width:
                continue
            for index in range(len(run) - width + 1):
                add(run[index : index + width], 1000 + width ** 2)
                if len(weighted) >= 320:
                    break
            if len(weighted) >= 320:
                break
        if len(weighted) >= 320:
            break

    return sorted(weighted.items(), key=lambda item: (item[1], len(item[0])), reverse=True)


def best_match(text: str, terms: list[tuple[str, int]], *, after: int = 0) -> tuple[int, int, str] | None:
    """Return the strongest literal query-term occurrence at/after ``after``."""

    haystack = text.casefold()
    best: tuple[int, int, int, str] | None = None
    for term, weight in terms:
        cursor = max(0, after)
        occurrences = 0
        while occurrences < 24:
            position = haystack.find(term, cursor)
            if position < 0:
                break
            candidate = (weight, len(term), position, term)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
            cursor = position + max(1, len(term))
            occurrences += 1
    if best is None:
        return None
    _, length, position, term = best
    return position, position + length, term


def diverse_matches(
    text: str,
    terms: list[tuple[str, int]],
    *,
    after: int = 0,
    limit: int = 3,
    minimum_byte_gap: int = 1152,
) -> list[tuple[int, int, str]]:
    """Return strong query-term anchors from distinct parts of a long page.

    Natural-language questions often contain several facets.  Returning only
    the first high-scoring occurrence can expose one facet while hiding a later
    scope condition or conclusion.  For every weighted term we therefore
    consider both its first and last occurrence, then greedily retain anchors
    that are far enough apart in the UTF-8 source to support separate windows.
    Long/quoted terms remain dominant, while overlapping CJK n-grams collapse
    into the same physical cluster.
    """

    if limit < 1:
        return []
    haystack = text.casefold()
    candidates: list[tuple[int, int, int, int, str]] = []
    for term, weight in terms:
        positions: list[int] = []
        cursor = max(0, after)
        while len(positions) < 32:
            position = haystack.find(term, cursor)
            if position < 0:
                break
            positions.append(position)
            cursor = position + max(1, len(term))
        if not positions:
            continue
        # Insurance PDFs commonly repeat a responsibility name in a contents
        # page before the operative clause.  Prefer the last occurrence so the
        # focused window reaches the clause rather than stopping at the TOC;
        # keep the first occurrence as a fallback diversity candidate.
        for evidence_bias, position in ((1, positions[-1]), (0, positions[0])):
            candidates.append((weight, len(term), evidence_bias, position, term))

    candidates.sort(reverse=True)
    chosen: list[tuple[int, int, str, int]] = []
    for _weight, length, _last_bias, position, term in candidates:
        position_byte = len(text[:position].encode("utf-8"))
        if any(abs(position_byte - existing_byte) < minimum_byte_gap for *_, existing_byte in chosen):
            continue
        chosen.append((position, position + length, term, position_byte))
        if len(chosen) >= limit:
            break
    return [(start, end, term) for start, end, term, _position_byte in chosen]


def byte_window(text: str, start_char: int, end_char: int, budget: int) -> tuple[str, int, int]:
    """Return a UTF-8-safe window around a character match."""

    data = text.encode("utf-8")
    match_start = len(text[:start_char].encode("utf-8"))
    match_end = len(text[:end_char].encode("utf-8"))
    if budget >= len(data):
        return text, 0, len(data)
    padding = max(0, budget - (match_end - match_start))
    start = max(0, match_start - padding // 2)
    end = min(len(data), start + budget)
    start = max(0, end - budget)
    while start < end and data[start] & 0xC0 == 0x80:
        start += 1
    fragment = data[start:end].decode("utf-8", errors="ignore")
    encoded = fragment.encode("utf-8")
    return fragment, start, start + len(encoded)


def bounded_byte_window(
    text: str,
    start_char: int,
    end_char: int,
    budget: int,
    *,
    lower_byte: int,
    upper_byte: int,
) -> tuple[str, int, int]:
    """Return a UTF-8 window within a non-overlapping source byte range."""

    data = text.encode("utf-8")
    lower = max(0, min(lower_byte, len(data)))
    upper = max(lower, min(upper_byte, len(data)))
    budget = max(0, min(budget, upper - lower))
    if budget == 0:
        return "", lower, lower
    match_start = len(text[:start_char].encode("utf-8"))
    match_end = len(text[:end_char].encode("utf-8"))
    padding = max(0, budget - (match_end - match_start))
    start = max(lower, match_start - padding // 2)
    end = min(upper, start + budget)
    start = max(lower, end - budget)
    while start < end and data[start] & 0xC0 == 0x80:
        start += 1
    fragment = data[start:end].decode("utf-8", errors="ignore")
    included = len(fragment.encode("utf-8"))
    return fragment, start, start + included


def select_content(data: bytes, query: str, budget: int) -> tuple[str, int, dict[str, object]]:
    """Select a bounded head, coherent query evidence, and source conclusion.

    Keeping the head preserves provenance and framing; a reasonably large
    query window preserves the surrounding argument/table; the tail preserves
    final qualifications and scope limits.  This is more reliable for evidence
    answering than many tiny hit snippets while remaining deterministic and
    byte bounded.
    """

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("page is not UTF-8")
    if len(data) <= budget:
        return text, len(data), {
            "mode": "full",
            "segments": [{"start_byte": 0, "end_byte": len(data)}],
            "matched_term": None,
            "matched_terms": [],
        }

    if budget < 2048:
        prefix, included = decode_prefix(data, budget)
        return prefix, included, {
            "mode": "prefix",
            "segments": [{"start_byte": 0, "end_byte": included}],
            "matched_term": None,
            "matched_terms": [],
        }

    focus_marker_template = "\n\n[... 已省略；以下为围绕查询词「{}」的原文片段 ...]\n\n"
    tail_marker = "\n\n[... 已省略；以下为该来源的文末原文 ...]\n\n"
    # A slightly larger head preserves titles, provenance, audience, and other
    # framing blocks that commonly precede the first body heading.
    head_budget = min(1536, max(768, budget // 4))
    tail_budget = min(1280, max(768, budget // 5))
    head, head_bytes = decode_prefix(data, head_budget)
    tail, tail_start, tail_bytes = decode_suffix(data, tail_budget)
    if tail_start <= head_bytes + 256:
        prefix, included = decode_prefix(data, budget)
        return prefix, included, {
            "mode": "prefix",
            "segments": [{"start_byte": 0, "end_byte": included}],
            "matched_term": None,
            "matched_terms": [],
        }

    terms = query_terms(query)
    # Bounded windows may begin exactly where the head ends, so a match just
    # past the head is useful rather than redundant (often the first body
    # conclusion following frontmatter/provenance).
    anchors = diverse_matches(text, terms, after=len(head), limit=6)
    anchors = [
        anchor
        for anchor in anchors
        if len(text[: anchor[0]].encode("utf-8")) < tail_start - 256
    ]

    tail_marker_bytes = len(tail_marker.encode("utf-8"))
    base_focus_budget = budget - head_bytes - tail_bytes - tail_marker_bytes
    window_count = 0
    if anchors and base_focus_budget >= 896:
        window_count = 1
    if len(anchors) >= 2 and base_focus_budget >= 6144:
        window_count = 2

    chosen: list[tuple[int, int, str]] = []
    if window_count:
        chosen.append(anchors[0])
        if window_count == 2:
            estimated_window = max(2048, base_focus_budget // 2)
            first_byte = len(text[: anchors[0][0]].encode("utf-8"))
            for anchor in anchors[1:]:
                anchor_byte = len(text[: anchor[0]].encode("utf-8"))
                if abs(anchor_byte - first_byte) >= estimated_window:
                    chosen.append(anchor)
                    break
            if len(chosen) < 2:
                window_count = 1
                chosen = chosen[:1]

    markers = [focus_marker_template.format(anchor[2]) for anchor in chosen]
    marker_bytes = sum(len(marker.encode("utf-8")) for marker in markers)
    focus_total_budget = max(0, base_focus_budget - marker_bytes)
    windows: list[tuple[int, int, str, str]] = []
    if chosen and focus_total_budget >= 768 * len(chosen):
        per_window = focus_total_budget // len(chosen)
        remainder = focus_total_budget % len(chosen)
        for index, anchor in enumerate(chosen):
            focus, focus_start, focus_end = bounded_byte_window(
                text,
                anchor[0],
                anchor[1],
                per_window + (1 if index < remainder else 0),
                lower_byte=head_bytes,
                upper_byte=tail_start,
            )
            windows.append((focus_start, focus_end, anchor[2], focus))
        windows.sort(key=lambda item: item[0])

    parts = [head]
    segments: list[dict[str, int]] = [{"start_byte": 0, "end_byte": head_bytes}]
    included_source_bytes = head_bytes
    matched_terms: list[str] = []
    for focus_start, focus_end, term, focus in windows:
        marker = focus_marker_template.format(term)
        parts.extend((marker, focus))
        segments.append({"start_byte": focus_start, "end_byte": focus_end})
        included_source_bytes += len(focus.encode("utf-8"))
        matched_terms.append(term)
    parts.extend((tail_marker, tail))
    segments.append({"start_byte": tail_start, "end_byte": len(data)})
    included_source_bytes += tail_bytes
    content = "".join(parts)
    if len(content.encode("utf-8")) > budget:
        raise ValueError("internal content selection exceeded byte budget")
    return content, included_source_bytes, {
        "mode": "head-query-and-tail" if windows else "head-and-tail",
        "segments": segments,
        "matched_term": matched_terms[0] if matched_terms else None,
        "matched_terms": matched_terms,
    }


parser = argparse.ArgumentParser()
parser.add_argument("--scope", required=True)
parser.add_argument("--vault")
parser.add_argument("--product-root")
parser.add_argument("--runtime-config")
parser.add_argument("--query", required=True)
parser.add_argument("--top", type=int, default=7)
parser.add_argument("--max-context-bytes", type=int, default=18 * 1024)
args = parser.parse_args()

if not 1 <= args.top <= 10:
    die("top must be between 1 and 10")
if not 4 * 1024 <= args.max_context_bytes <= 64 * 1024:
    die("max-context-bytes must be between 4096 and 65536")
if not args.query.strip() or len(args.query) > 8000:
    die("query must contain 1 to 8000 characters")

scope_path = resolved(args.scope)
try:
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    die(f"cannot read scope: {exc}")
if scope.get("schema") not in {"llm-wiki.privacy-scope.v1", "llm-wiki.privacy-scope.v2"}:
    die("unsupported privacy scope schema")
if scope.get("model_context_egress_approved") is not True:
    die("model context egress is not approved for this scope")
if scope.get("mode") not in {"query-only", "query-and-ingest"}:
    die("scope mode must be query-only or query-and-ingest")

try:
    allowed_vaults = [resolved(value) for value in scope.get("allowed_vaults", [])]
    forbidden = [resolved(value, must_exist=False) for value in scope.get("forbidden_roots", [])]
except (OSError, RuntimeError) as exc:
    die(f"cannot resolve authorized path: {exc}")

if len(allowed_vaults) != 1 and not args.vault:
    die("scope must contain exactly one allowed vault when --vault is omitted")
try:
    vault = resolved(args.vault) if args.vault else allowed_vaults[0]
except (OSError, RuntimeError) as exc:
    die(f"cannot resolve vault: {exc}")

skill_root = Path(__file__).resolve().parents[1]
product_value = args.product_root
runtime_candidates: list[Path] = []
if args.runtime_config:
    runtime_candidates.append(resolved(args.runtime_config))
runtime_candidates.append(skill_root / "runtime.json")
if not product_value:
    for candidate in runtime_candidates:
        if not candidate.is_file():
            continue
        try:
            runtime = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            die(f"cannot read runtime config {candidate}: {exc}")
        product_value = runtime.get("product_root")
        if product_value:
            break
if not product_value:
    vendored = skill_root / "vendor/claude-obsidian"
    if vendored.is_dir():
        product_value = str(vendored)
if not isinstance(product_value, str):
    die("product root is not configured; reinstall the skill runtime")
try:
    product_root = resolved(product_value)
except (OSError, RuntimeError) as exc:
    die(f"cannot resolve product root: {exc}")

if not inside(vault, allowed_vaults):
    die("vault is outside allowed_vaults")
if inside(vault, forbidden):
    die("vault conflicts with forbidden_roots")
if not (vault / ".claude-obsidian.json").is_file() or not (vault / "wiki").is_dir():
    die("selected path is not an initialized claude-obsidian vault")

retrieve = product_root / "scripts/retrieve.py"
if not retrieve.is_file():
    die("claude-obsidian retrieve.py is missing")
command = [
    sys.executable,
    str(retrieve),
    "--vault",
    str(vault),
    args.query,
    "--top",
    str(min(40, args.top * 4)),
    "--no-rerank",
    "--explain",
]
completed = subprocess.run(command, text=True, capture_output=True)
if completed.returncode != 0:
    die(f"retrieval failed with exit {completed.returncode}: {completed.stderr.strip()}", completed.returncode)
start = completed.stdout.find("{")
if start < 0:
    die("retrieval returned no JSON")
try:
    result = json.loads(completed.stdout[start:])
except ValueError as exc:
    die(f"retrieval JSON is invalid: {exc}")

selected: list[tuple[dict, str, bytes]] = []
seen_pages: set[str] = set()
filtered_pages: list[dict[str, str]] = []
superseded_pages = superseded_source_pages(vault)
current_source_pages = active_source_pages(vault)
include_history = asks_for_history(args.query)
include_navigation = asks_for_navigation(args.query)
derived_navigation_pages = {
    "wiki/index.md",
    "wiki/hot.md",
    "wiki/log.md",
    "wiki/overview.md",
    "wiki/sources/来源导航.md",
}
expanded_candidates: list[dict] = []
graph_promotions: list[dict[str, object]] = []
wrapper_order = 0
for candidate in result.get("candidates", []):
    relative = candidate.get("page_path")
    if not isinstance(relative, str) or not relative.startswith("wiki/"):
        die("retrieval returned an invalid wiki path")
    if relative.startswith("wiki/知识图谱/") and not include_navigation:
        filtered_pages.append({"page_path": relative, "reason": "derived-graph-page"})
        promoted = graph_evidence_pages(vault, relative, current_source_pages)
        graph_promotions.append({"graph_page": relative, "source_pages": promoted})
        graph_score = float(candidate.get("bm25_score") or 0.0)
        # A matching graph node is strong routing evidence, but a long node's
        # third listed source must not automatically outrank a source that also
        # matches the query directly.  Rank decay retains the graph's first
        # canonical source while allowing direct BM25 evidence and a second
        # matching graph node to establish the best cross-document order.
        promotion_decay = (0.96, 0.62, 0.48, 0.38, 0.32, 0.28)
        for source_index, source in enumerate(promoted):
            decay = promotion_decay[min(source_index, len(promotion_decay) - 1)]
            expanded_candidates.append({
                "page_path": source,
                "page_address": None,
                "bm25_score": candidate.get("bm25_score"),
                "snippet": f"由中文图谱节点 {relative} 路由到当前来源页。",
                "promoted_by_graph": relative,
                "_wrapper_effective_score": graph_score * decay,
                "_wrapper_order": wrapper_order,
            })
            wrapper_order += 1
        continue
    direct = dict(candidate)
    direct["_wrapper_effective_score"] = float(candidate.get("bm25_score") or 0.0)
    direct["_wrapper_order"] = wrapper_order
    wrapper_order += 1
    expanded_candidates.append(direct)

expanded_candidates.sort(
    key=lambda candidate: (
        float(candidate.get("_wrapper_effective_score") or 0.0),
        -int(candidate.get("_wrapper_order") or 0),
    ),
    reverse=True,
)

for candidate in expanded_candidates:
    relative = candidate.get("page_path")
    if not isinstance(relative, str) or not relative.startswith("wiki/"):
        die("expanded retrieval returned an invalid wiki path")
    if relative in seen_pages:
        continue
    seen_pages.add(relative)
    if relative in superseded_pages and not include_history:
        filtered_pages.append({"page_path": relative, "reason": "superseded-source-version"})
        continue
    if relative in derived_navigation_pages and not include_navigation:
        filtered_pages.append({"page_path": relative, "reason": "derived-navigation-page"})
        continue
    page = (vault / relative).resolve(strict=True)
    if not inside(page, [(vault / "wiki").resolve()]) or inside(page, forbidden):
        die("retrieval page escaped the authorized wiki root")
    data = page.read_bytes()
    if len(data) > 128 * 1024:
        die(f"retrieved page is too large: {relative}")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        die(f"retrieved page is not UTF-8: {relative}")
    selected.append((candidate, relative, data))

# The retrieval overfetch above protects evidence recall when derived graph and
# navigation pages rank ahead of sources. Restore the caller's requested result
# count only after those derived pages and superseded versions are filtered.
selected = selected[:args.top]

# Prevent one long first result from consuming the complete egress budget. Each
# included page receives at least 1 KiB; the default 18 KiB budget includes all
# seven default results with a focused evidence window per page.
max_pages_by_budget = max(1, args.max_context_bytes // 1024)
omitted_by_budget = max(0, len(selected) - max_pages_by_budget)
selected = selected[:max_pages_by_budget]
pages = []
total_bytes = 0
page_count = len(selected)
try:
    page_budgets = allocate_page_budgets(args.max_context_bytes, page_count)
except ValueError as exc:
    die(str(exc))
for page_budget, (candidate, relative, data) in zip(page_budgets, selected):
    try:
        content, included_source_bytes, selection = select_content(data, args.query, page_budget)
    except ValueError:
        die(f"retrieved page is not UTF-8: {relative}")
    content_bytes = len(content.encode("utf-8"))
    total_bytes += content_bytes
    pages.append(
        {
            "page_path": relative,
            "page_address": candidate.get("page_address") or address_from_page(data),
            "bm25_score": candidate.get("bm25_score"),
            "retrieval_snippet": candidate.get("snippet"),
            "content_sha256": hashlib.sha256(data).hexdigest(),
            "content_total_bytes": len(data),
            "content_included_bytes": included_source_bytes,
            "content_egress_bytes": content_bytes,
            "content_truncated": included_source_bytes < len(data),
            "content_selection": selection,
            "content": content,
        }
    )

output = {
    "schema": "llm-wiki.scoped-query-result.v2",
    "vault": str(vault),
    "query": args.query,
    "strategy": result.get("strategy"),
    "network_used_by_wrapper": False,
    "vault_mutation_allowed": False,
    "scope_mode": scope.get("mode"),
    "context_budget_bytes": args.max_context_bytes,
    "context_included_bytes": total_bytes,
    "context_egress_bytes": total_bytes,
    "candidate_pages": len(seen_pages),
    "filtered_pages": filtered_pages,
    "graph_promotions": graph_promotions,
    "pages_omitted_by_budget": omitted_by_budget,
    "pages": pages,
}
print(json.dumps(output, ensure_ascii=False, indent=2))
