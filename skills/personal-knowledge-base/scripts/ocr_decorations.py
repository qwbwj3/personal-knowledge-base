"""Narrow OCR score exceptions backed by selectable text and visual row context.

This does not classify images as harmless: every bitmap still requires OCR.
Unpositioned lines, scan-only pages and unknown glyphs remain fail-closed.
Raw OCR text/boxes/scores are never removed or corrected.
"""
from __future__ import annotations

import math
import re
import json
import os
from pathlib import Path
import subprocess

BULLETS = frozenset('◆■●•')
# Only the observed symbol-font bullet mappings, never arbitrary PUA prose.
PUA_BULLETS = frozenset({0xf06c, 0xf075, 0xf0af})
DOTS = '.·…．⋯'


def _compact(text):
    return ''.join(str(text).split())


def _without_leading_decorations(text):
    """Drop this module's own known section markers from the line start.

    Structure headings reach us with the marker the document's symbol font puts
    in front of them (for example U+F075 immediately before 条款目录), so the
    compacted row is '\uf075条款目录' and never equals the heading itself. Reuse
    the glyph set that already backs the bullet score exceptions instead of
    inventing a new one.

    This is deliberately narrower than any content decision: it only removes
    glyphs this module already treats as decoration, the remainder must still
    equal a known heading exactly, and it relaxes no OCR score, no reference row
    and no two-row minimum. Rows that do not begin with such a glyph are
    returned untouched, so unrelated text cannot become a heading.
    """
    remaining = text
    while remaining and (remaining[0] in BULLETS or ord(remaining[0]) in PUA_BULLETS):
        remaining = remaining[1:]
    return remaining


def _box(line):
    try:
        points = line['box']
        if len(points) != 4:
            return None
        xs, ys = zip(*[(float(p[0]), float(p[1])) for p in points])
        if not all(math.isfinite(v) for v in (*xs, *ys)):
            return None
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _trusted(line):
    try:
        value = float(line['confidence'])
        return math.isfinite(value) and .92 <= value <= 1
    except (KeyError, TypeError, ValueError):
        return False


def _same_row(a, b):
    # Require substantial overlap and similar height; nearby table rows do not
    # supply evidence for a glyph in a different row.
    ha, hb = a[3] - a[1], b[3] - b[1]
    return (max(ha, hb) <= min(ha, hb) * 2.5 and
            min(a[3], b[3]) - max(a[1], b[1]) >= min(ha, hb) * .6)


def score_exemptions(ocr, native_text, mapping, context=None):
    """Return line-index -> auditable reason; never exempt content characters.

    A single bullet must precede high-confidence body text on the same OCR row,
    matching a bullet-prefixed native line. Dot leaders require the native title
    and page number AND matching OCR title/page-number cells on the same row.
    One native occurrence can justify at most one exception.
    """
    if not native_text or not mapping or mapping.get('invalid_mapping') or mapping.get('warnings'):
        return {}
    # A pure OCR symbol inside a bitmap could be a misread small amount or
    # negation. Never exempt it. Only inspectable native page coordinates may
    # support exceptions; rotated/cropped pages take the ordinary strict gate.
    if not context:
        return {}
    coordinates = ocr.get('coordinates') or {}
    try:
        scale = float(coordinates['render_scale'])
        page_width, page_height = float(context['width']), float(context['height'])
        if (coordinates.get('coordinate_space') != 'rendered_page_pixels'
                or not math.isfinite(scale) or scale <= 0
                or abs(float(coordinates['width']) - page_width * scale) > 1.1
                or abs(float(coordinates['height']) - page_height * scale) > 1.1):
            return {}
    except (KeyError, ValueError, TypeError):
        return {}
    symbols = set(mapping.get('symbol_font_codepoints', []))
    native_bullets = []
    native_leaders = []
    for raw in native_text.splitlines():
        value = raw.strip()
        if not value:
            continue
        while value and value[0] == '\uf020' and 0xf020 in symbols:
            value = value[1:].lstrip()
        if not value:
            continue
        first = value[0]
        if first in BULLETS or ord(first) in (symbols & PUA_BULLETS):
            # Symbol-font PUA spaces sometimes follow the bullet. They can be
            # skipped only when this font explicitly maps these codepoints.
            rest = value[1:]
            while rest and (rest[0].isspace() or (rest[0] == '\uf020' and 0xf020 in symbols)):
                rest = rest[1:]
            compact = _compact(rest)
            if sum(c.isalnum() for c in compact) >= 4:
                native_bullets.append((first, compact))
        leader = re.fullmatch(r'(.+?)([.·…．⋯\s]{3,})(\d{1,4})', value)
        if leader and sum(c in DOTS for c in leader[2]) >= 3:
            title = _compact(leader[1])
            if sum(c.isalnum() for c in title) >= 4:
                native_leaders.append((title, leader[3]))
    lines = ocr.get('lines') or []
    boxes = [_box(line) for line in lines]
    exemptions, used_bullets, used_leaders = {}, set(), set()
    for index, line in enumerate(lines):
        text = _compact(line.get('text', ''))
        box = boxes[index]
        if not box:
            continue
        native_box = (box[0] / scale, page_height - box[3] / scale,
                      box[2] / scale, page_height - box[1] / scale)
        # Include one PDF point of safety at image edges.
        if any(native_box[0] < r[2] + 1 and native_box[2] > r[0] - 1
               and native_box[1] < r[3] + 1 and native_box[3] > r[1] - 1
               for r in context.get('bitmap_rectangles_pt', [])):
            continue
        height = box[3] - box[1]
        # Do not remove mixed lines, numbers, dashes/minus signs, punctuation
        # with prose, arbitrary PUA runs, or several ambiguous symbols at once.
        if len(text) == 1 and text in BULLETS:
            if box[2] - box[0] > height * 2:
                continue
            neighbors = [(j, _compact(other.get('text', ''))) for j, other in enumerate(lines)
                         if j != index and boxes[j] and _trusted(other) and _same_row(box, boxes[j])
                         and 0 <= boxes[j][0] - box[2] <= height * 3]
            for j, body in neighbors:
                if sum(c.isalnum() for c in body) < 4:
                    continue
                for n, (glyph, native_body) in enumerate(native_bullets):
                    if n not in used_bullets and body == native_body and (glyph == text or ord(glyph) in (symbols & PUA_BULLETS)):
                        exemptions[index] = {'kind': 'native_list_bullet', 'native_glyph': glyph,
                                             'native_context': native_body, 'ocr_context_line': j,
                                             'basis': 'mapped_native_prefix_and_matching_positioned_ocr_body'}
                        used_bullets.add(n)
                        break
                if index in exemptions:
                    break
        elif len(text) >= 3 and all(c in DOTS for c in text):
            left = [(j, _compact(other.get('text', ''))) for j, other in enumerate(lines)
                    if j != index and boxes[j] and _trusted(other) and _same_row(box, boxes[j])
                    and 0 <= box[0] - boxes[j][2] <= height * 3]
            right = [(j, _compact(other.get('text', ''))) for j, other in enumerate(lines)
                     if j != index and boxes[j] and _trusted(other) and _same_row(box, boxes[j])
                     and 0 <= boxes[j][0] - box[2] <= height * 3]
            for n, (title, page_number) in enumerate(native_leaders):
                if n in used_leaders:
                    continue
                pair = next(((a, b) for a, t in left for b, p in right if t == title and p == page_number), None)
                if pair:
                    exemptions[index] = {'kind': 'native_contents_dot_leader', 'native_context': title,
                                         'native_page_number': page_number, 'ocr_context_lines': list(pair),
                                         'basis': 'native_leader_and_matching_positioned_ocr_title_and_page_number'}
                    used_leaders.add(n)
                    break
    return exemptions


# Reference numbers are NOT score-exempt decorations. Their distinct admission
# path uses positioned native PDF text as evidence and leaves OCR scores intact.
_REFERENCE = r"[0-9]{1,3}(?:\.[0-9]{1,3}){0,2}"
_MERGED = re.compile(r"(?P<leader>[.·…．⋯]{3,})(?P<reference>" + _REFERENCE + r")")
_NATIVE_REFERENCE = re.compile(r"(?P<title>.+?)(?P<leader>[.·…．⋯]{3,})(?P<reference>" + _REFERENCE + r")")
_CONTENT_HEADINGS = frozenset(('目录', '条款目录', '条款阅读指引', 'contents', 'tableofcontents'))


def needs_reference_geometry(ocr, native_text=None):
    """Only mixed leader/reference rows below the existing minimum need work."""
    if native_text is not None and not _native_reference_rows(native_text):
        return False
    return any(_MERGED.fullmatch(_compact(line.get('text', ''))) and
               isinstance(line.get('confidence'), (int, float)) and
               math.isfinite(line['confidence']) and 0 <= line['confidence'] < .80
               for line in ocr.get('lines', []))


def _native_reference_rows(native_text):
    compact_lines = [_compact(row) for row in native_text.splitlines()]
    # Headings carry the symbol-font section marker; strip only known markers so
    # the contents structure is still recognised. Row text is matched unchanged.
    headings = [_without_leading_decorations(row) for row in compact_lines]
    if not any(row.casefold() in _CONTENT_HEADINGS for row in headings):
        return []
    rows = []
    for row in compact_lines:
        match = _NATIVE_REFERENCE.fullmatch(row)
        if match and sum(c.isalnum() for c in match['title']) >= 4:
            rows.append((match['title'], match['reference'], row))
    # One isolated decimal after dots is not proof of a contents structure.
    return rows if len({(title, ref) for title, ref, _ in rows}) >= 2 else []


def _pdfium_reference_geometry(path, page_index):
    """Exact character boxes, not estimated font widths or OCR-derived boxes."""
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(path)) as document:
        if not 0 <= page_index < len(document):
            raise ValueError('Invalid PDF page index')
        page = document[page_index]
        try:
            width, height = page.get_size()
            textpage = page.get_textpage()
            try:
                count = textpage.count_chars()
                if count > 50000:
                    raise ValueError('Page geometry character limit exceeded')
                rows, chars = [], []
                for i in range(count):
                    char = textpage.get_text_range(i, 1)
                    if len(char) != 1:
                        raise ValueError('Character index/text mismatch')
                    if char in '\r\n':
                        if chars:
                            rows.append(chars); chars = []
                    else:
                        box = list(textpage.get_charbox(i))
                        if len(box) != 4 or not all(math.isfinite(v) for v in box):
                            raise ValueError('Invalid native character geometry')
                        chars.append({'text': char, 'box': box})
                if chars:
                    rows.append(chars)
                # Keep only small, structurally relevant rows; never return a
                # page's full character inventory in extraction metadata.
                references = []
                for row in rows:
                    value = ''.join(char['text'] for char in row)
                    if _NATIVE_REFERENCE.fullmatch(_compact(value)) and len(value) <= 1000:
                        references.append({'text': value, 'characters': row})
                return {'reader': 'pdfium_native_char_geometry', 'width': width,
                        'height': height, 'reference_rows': references}
            finally:
                textpage.close()
        finally:
            page.close()


def load_reference_geometry(path, page_index):
    """Use installed readers, including the isolated OCR runtime, without download."""
    try:
        return _pdfium_reference_geometry(path, page_index)
    except ImportError:
        import ocr_runtime
        return ocr_runtime.read_native_pdf(path, page_index, mode='geometry')


def _reference_corroborations(ocr, native_text, mapping, context=None):
    """Low OCR confidence stays low; matching native references are explicit.

    Require: a native contents heading and >= 2 native leader/reference rows;
    exact primary/independent native text agreement; high-score OCR title on the
    same row; every reference character's actual native box within the OCR tail;
    and no image overlap. This never certifies OCR numeric/semantic accuracy.
    """
    if (not native_text or not mapping or mapping.get('invalid_mapping') or mapping.get('warnings')
            or not context or not needs_reference_geometry(ocr)):
        return {}
    structure = _native_reference_rows(native_text)
    geometry = context.get('native_reference_geometry') or {}
    coordinates = ocr.get('coordinates') or {}
    try:
        scale = float(coordinates['render_scale'])
        width, height = float(context['width']), float(context['height'])
        if (coordinates.get('coordinate_space') != 'rendered_page_pixels'
                or not math.isfinite(scale) or scale <= 0
                or abs(float(coordinates['width']) - width * scale) > 1.1
                or abs(float(coordinates['height']) - height * scale) > 1.1
                or not all(math.isfinite(v) and v > 0 for v in (width, height, float(geometry['width']), float(geometry['height'])))
                or geometry.get('reader') != 'pdfium_native_char_geometry'
                or abs(float(geometry['width']) - width) > .01
                or abs(float(geometry['height']) - height) > .01):
            return {}
    except (KeyError, TypeError, ValueError):
        return {}
    if not structure:
        return {}
    positioned = []
    for row in geometry.get('reference_rows', []):
        chars = row.get('characters', [])
        if ''.join(char.get('text', '') for char in chars) != row.get('text'):
            continue
        compact_chars = [char for char in chars if not char.get('text', '').isspace()]
        compact = ''.join(char.get('text', '') for char in compact_chars)
        matches = [(title, ref) for title, ref, value in structure if value == compact]
        if len(matches) != 1:
            continue
        title, ref = matches[0]
        # Duplicate identical native rows are ambiguous: do not select a nearby
        # number from another section merely because its value happens to match.
        if sum(_compact(other.get('text', '')) == compact for other in geometry.get('reference_rows', [])) != 1:
            continue
        try:
            boxes = [char['box'] for char in compact_chars]
            if any(len(box) != 4 or not all(math.isfinite(v) for v in box)
                   or box[2] <= box[0] or box[3] <= box[1] for box in boxes):
                continue
        except (KeyError, TypeError):
            continue
        positioned.append((title, ref, boxes[:len(title)], boxes[-len(ref):], compact))
    # Independent geometry must confirm the contents pattern, not just one row.
    if len(positioned) < 2:
        return {}
    results, used = {}, set()
    lines = ocr.get('lines', [])
    for i, line in enumerate(lines):
        match = _MERGED.fullmatch(_compact(line.get('text', '')))
        box = _box(line)
        score = line.get('confidence')
        if (not match or not box or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score < .80):
            continue
        h = box[3] - box[1]
        native_box = (box[0] / scale, height - box[3] / scale, box[2] / scale, height - box[1] / scale)
        if any(native_box[0] < r[2] + 1 and native_box[2] > r[0] - 1
               and native_box[1] < r[3] + 1 and native_box[3] > r[1] - 1
               for r in context.get('bitmap_rectangles_pt', [])):
            continue
        for title, ref, title_boxes, ref_boxes, native_row in positioned:
            if native_row in used or ref != match['reference']:
                continue
            # All reference glyphs (including the '.' inside 1.7) must occupy
            # the same row and the rightmost part of the mixed OCR line.
            tol = min(2., h / scale * .20)
            if not all(native_box[0] - tol <= b[0] and b[2] <= native_box[2] + tol
                       and native_box[1] - tol <= b[1] and b[3] <= native_box[3] + tol
                       for b in ref_boxes):
                continue
            if abs(ref_boxes[-1][2] - native_box[2]) > h / scale:
                continue
            title_union = (min(b[0] for b in title_boxes), min(b[1] for b in title_boxes),
                           max(b[2] for b in title_boxes), max(b[3] for b in title_boxes))
            for j, other in enumerate(lines):
                other_box = _box(other)
                if (j == i or not other_box or not _trusted(other) or _compact(other.get('text', '')) != title
                        or not _same_row(box, other_box) or not 0 <= box[0] - other_box[2] <= h * 3):
                    continue
                ot = (other_box[0] / scale, height - other_box[3] / scale,
                      other_box[2] / scale, height - other_box[1] / scale)
                if any(ot[0] < r[2] + 1 and ot[2] > r[0] - 1
                       and ot[1] < r[3] + 1 and ot[3] > r[1] - 1
                       for r in context.get('bitmap_rectangles_pt', [])):
                    continue
                if not (abs(ot[0] - title_union[0]) <= h / scale
                        and abs(ot[2] - title_union[2]) <= h / scale
                        and ot[1] - tol <= title_union[1] and title_union[3] <= ot[3] + tol):
                    continue
                results[i] = {'status': 'native_corroborated', 'kind': 'contents_reference',
                              'reference': ref, 'reference_role': 'contents_reference_not_business_amount',
                              'native_context': title, 'native_reference_boxes_pt': ref_boxes,
                              'ocr_context_line': j, 'raw_ocr_confidence': line.get('confidence'),
                              'ocr_numeric_accuracy_verified': False, 'semantic_accuracy_verified': False,
                              'basis': 'matching_primary_and_positioned_native_text_with_same_row_ocr_title'}
                used.add(native_row)
                break
            if i in results:
                break
    return results


def reference_corroborations(ocr, native_text, mapping, context=None, diagnostics=None):
    result = _reference_corroborations(ocr, native_text, mapping, context)
    if diagnostics is not None:
        candidates = [i for i, line in enumerate(ocr.get('lines') or [])
                      if needs_reference_geometry({'lines': [line]})]
        geometry = (context or {}).get('native_reference_geometry') or {}
        if not candidates:
            reason = 'no_low_confidence_mixed_reference'
        elif not native_text:
            reason = 'trusted_native_text_unavailable'
        elif not mapping or mapping.get('invalid_mapping') or mapping.get('warnings'):
            reason = 'native_mapping_untrusted'
        elif not context:
            reason = 'page_coordinate_context_unsupported'
        elif not _native_reference_rows(native_text):
            reason = 'native_contents_structure_unconfirmed'
        elif not geometry:
            reason = 'native_reference_geometry_unavailable'
        elif len(result) != len(candidates):
            reason = 'independent_text_or_character_alignment_unconfirmed'
        else:
            reason = 'native_references_corroborated'
        diagnostics.update(schema='personal-kb.reference-diagnostic.v1', reason_code=reason,
                           candidate_line_indices=candidates, corroborated_line_indices=sorted(result),
                           unresolved_line_indices=[i for i in candidates if i not in result],
                           native_reference_row_count=len(_native_reference_rows(native_text or '')),
                           independent_reference_row_count=len(geometry.get('reference_rows') or []),
                           thresholds_changed=False, semantic_accuracy_verified=False)
    return result


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 4 or sys.argv[1] != '--native-geometry':
        raise SystemExit('Expected --native-geometry PDF_PATH PAGE_INDEX')
    print(json.dumps(_pdfium_reference_geometry(Path(sys.argv[2]), int(sys.argv[3])), ensure_ascii=False))
