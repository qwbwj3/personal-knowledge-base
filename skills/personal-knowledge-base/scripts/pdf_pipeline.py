"""Page-aware local extraction. Readability is a gate, never a factual certificate.

Text first; font-code coverage detects nonempty garbage without word allowlists.
An alternate reader is attempted before lazy, explicitly provisioned local OCR.
No names, insurance phrases, fixture hashes or OS identity enter quality decisions.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from ocr_decorations import score_exemptions, needs_reference_geometry, load_reference_geometry, reference_corroborations
from installer_process import quiet_subprocess_kwargs
from owned_process import run as run_owned

def _raise_if_uncontained(exc):
    if getattr(exc, "abort_operation", False):
        raise exc


EXTRACTION_VERSION = "pdf-quality-v5-visual-review"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_PAGES = 500


def quality(text: str, symbol_codes=()) -> dict:
    chars = [c for c in text if not c.isspace()]
    # A lone private-use glyph at a list-line boundary is commonly an embedded
    # symbol-font bullet, not a run of unreadable prose. Preserve it and warn.
    decorative = len(re.findall(r"(?m)^[ \t]*[\ue000-\uf8ff][ \t]+(?=\S)", text))
    # Declared symbol-font PUA prefixes can include mapped spaces and multiple
    # bullets. Only exclude codes actually mapped by a symbol font at a line
    # boundary; never whitelist a product/file/phrase or arbitrary PUA prose.
    if symbol_codes:
        symbols = set(symbol_codes)
        decorative = 0
        for line in text.splitlines():
            prefix = re.match(r"^[ \t]*([\ue000-\uf8ff \t]+)(?=[^\ue000-\uf8ff \t])", line)
            if prefix:
                decorative += sum(ord(c) in symbols for c in prefix.group(1))
    bad = sum(c == "\ufffd" or unicodedata.category(c) in {"Co", "Cs", "Cc"} for c in chars) - decorative
    cid = len(re.findall(r"\(cid:\d+\)", text))
    reasons = []
    if not chars:
        reasons.append("empty_text")
    if bad and (bad >= 3 or bad / max(1, len(chars)) > .02):
        reasons.append("invalid_or_private_unicode")
    if cid:
        reasons.append("unmapped_cid_text")
    return {"readable": not reasons, "characters": len(chars), "invalid_characters": bad, "unverified_list_glyphs": decorative,
            "reasons": reasons, "semantic_accuracy_verified": False}


def _object(value):
    return value.get_object() if hasattr(value, "get_object") else value


def _cmap_ranges(data: bytes) -> list[tuple[int, int]]:
    # Only source-code columns count, not destination Unicode values. Codespace
    # ranges define syntax, not actual glyph mappings and cannot grant coverage.
    ranges = []
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for a in re.findall(rb"<([0-9A-Fa-f]+)>\s*<[^>]+>", block):
            value = int(a, 16); ranges.append((value, value))
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for a, b in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<[^>]+>|\[[^\]]*\])", block, re.S):
            ranges.append((int(a, 16), int(b, 16)))
    return ranges


def font_coverage(page, reader) -> dict:
    """Check codes actually painted by Identity CID fonts, including Form XObjects.

    Ordinary Type1 fonts/known encodings are not rejected for lacking ToUnicode.
    Failing to inspect a stream is explicit, not a claim that its maps are sound.
    """
    from pypdf.generic import ContentStream
    used = unmapped = 0
    faults = []
    seen = set()
    font_maps = {}
    symbol_codes = set()
    def walk(stream, resources, depth=0):
        nonlocal used, unmapped
        if depth > 12:
            faults.append("form_depth_limit"); return
        resources = _object(resources) or {}
        fonts = _object(resources.get("/Font", {})) or {}
        font_name = None
        font_stack = []
        for operands, operator in ContentStream(stream, reader).operations:
            if operator == b"q":
                font_stack.append(font_name)
            elif operator == b"Q":
                font_name = font_stack.pop() if font_stack else None
            elif operator == b"Tf":
                font_name = operands[0]
            elif operator == b"Do":
                forms = _object(resources.get("/XObject", {})) or {}
                form = _object(forms.get(operands[0]))
                if form is not None and form.get("/Subtype") == "/Form" and id(form) not in seen:
                    seen.add(id(form)); walk(form, form.get("/Resources", resources), depth + 1)
            elif operator in {b"Tj", b"TJ", b"'", b'"'}:
                font = _object(fonts.get(font_name))
                if not font or font.get("/Subtype") != "/Type0" or str(font.get("/Encoding")) not in {"/Identity-H", "/Identity-V"}:
                    continue
                key = id(font)
                if key not in font_maps:
                    cmap = _object(font.get("/ToUnicode"))
                    cmap_data = cmap.get_data() if cmap is not None else b""
                    font_maps[key] = (_cmap_ranges(cmap_data), {})
                    family = str(font.get("/BaseFont", "")).split("+")[-1].lower()
                    if any(family.startswith(n) for n in ("wingdings", "webdings", "zapfdingbats", "symbol")):
                        for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap_data, re.S):
                            for target in re.findall(rb"<[^>]+>\s*<([0-9A-Fa-f]{4})>", block):
                                code = int(target,16)
                                if 0xe000 <= code <= 0xf8ff: symbol_codes.add(code)
                ranges, mapped_codes = font_maps[key]
                values = operands[0] if operator == b"TJ" else [operands[-1]]
                for value in values:
                    if isinstance(value, (int, float)):
                        continue
                    try:
                        raw = value.original_bytes if hasattr(value, "original_bytes") else bytes(value)
                    except (TypeError, UnicodeError):
                        faults.append("font_code_unavailable"); continue
                    if len(raw) % 2:
                        faults.append("odd_identity_code_length")
                    for i in range(0, len(raw) - 1, 2):
                        code = int.from_bytes(raw[i:i+2], "big")
                        used += 1
                        if code not in mapped_codes:
                            mapped_codes[code] = any(lo <= code <= hi for lo, hi in ranges)
                        if not mapped_codes[code]:
                            unmapped += 1
    try:
        walk(page.get_contents(), page.get("/Resources", {}))
    except Exception as exc:
        _raise_if_uncontained(exc)
        faults.append("font_inspection_failed:" + type(exc).__name__)
    return {"identity_codes": used, "unmapped_codes": unmapped,
            "invalid_mapping": bool(unmapped), "symbol_font_codepoints": sorted(symbol_codes), "warnings": sorted(set(faults))}


def image_coverage(page, reader) -> dict:
    """Inventory painted bitmaps; area is diagnostic, never proof of no text.

    Even a small logo may contain words. Unknown inspection is fail-closed and
    requires the same whole-page OCR path as a detected bitmap.
    """
    from pypdf.generic import ContentStream
    identity = (1., 0., 0., 1., 0., 0.)
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    areas = []
    rectangles = []
    def mul(m, n):
        a,b,c,d,e,f=m; g,h,i,j,k,l=n
        return (a*g+b*i,a*h+b*j,c*g+d*i,c*h+d*j,e*g+f*i+k,e*h+f*j+l)
    def walk(stream, resources, ctm=identity, depth=0):
        if depth>12:raise RuntimeError("image_form_depth_limit")
        stack=[]; resources=_object(resources) or {}
        for operands,op in ContentStream(stream,reader).operations:
            if op==b'q':stack.append(ctm)
            elif op==b'Q':ctm=stack.pop() if stack else identity
            elif op==b'cm':ctm=mul(tuple(map(float,operands)),ctm)
            elif op in {b'Do',b'INLINE IMAGE'}:
                obj = _object((_object(resources.get('/XObject',{})) or {}).get(operands[0])) if op==b'Do' else None
                if op==b'INLINE IMAGE' or (obj is not None and obj.get('/Subtype')=='/Image'):
                    a,b,c,d,e,f=ctm; points=[(e,f),(a+e,b+f),(c+e,d+f),(a+c+e,b+d+f)]
                    xs,ys=zip(*points)
                    area=max(0,min(width,max(xs))-max(0,min(xs)))*max(0,min(height,max(ys))-max(0,min(ys)))
                    areas.append(area/max(1,width*height))
                    if area: rectangles.append((max(0,min(xs)),max(0,min(ys)),min(width,max(xs)),min(height,max(ys))))
                elif obj is not None and obj.get('/Subtype')=='/Form':
                    walk(obj,obj.get('/Resources',resources),mul(tuple(map(float,obj.get('/Matrix',identity))),ctm),depth+1)
    try:
        walk(page.get_contents(),page.get('/Resources',{}))
        # Rectangle union, not maximum/sum: tiled scans must be detected while
        # overlapping copies of one small logo must not inflate coverage.
        cuts = sorted({x for x0,y0,x1,y1 in rectangles for x in (x0,x1)})
        total = 0.
        for left,right in zip(cuts,cuts[1:]):
            spans = sorted((y0,y1) for x0,y0,x1,y1 in rectangles if x0 < right and x1 > left)
            covered = 0.; end = 0.
            for y0,y1 in spans:
                covered += max(0,y1-max(y0,end)); end=max(end,y1)
            total += (right-left)*covered
        return {'largest_image_fraction':round(max(areas or [0]),4), 'total_image_fraction':round(total/max(1,width*height),4),'image_count':len(areas), 'image_rectangles_pt': [list(r) for r in rectangles]}
    except Exception as exc:
        _raise_if_uncontained(exc)
        return {'largest_image_fraction':0,'total_image_fraction':0,'warning':'image_inspection_failed:'+type(exc).__name__}


def _alternate(path: Path, index: int) -> tuple[str, str]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None
    if pdfium is not None:
        with pdfium.PdfDocument(str(path)) as doc:
            page = doc[index]
            try:
                tp = page.get_textpage()
                try:
                    return tp.get_text_range(), "pdfium-text"
                finally:
                    tp.close()
            finally:
                page.close()
    import ocr_runtime
    if ocr_runtime.probe().get('ready'):
        value = ocr_runtime.read_native_pdf(path, index, mode='text')
        return value['text'], 'pdfium-runtime-text'
    executable = shutil.which('pdftotext')
    if executable:
        cp = run_owned([executable, '-f', str(index+1), '-l', str(index+1), '-enc', 'UTF-8',
                        '-layout', str(path), '-'], timeout=120)
        if cp.returncode == 0:
            return cp.stdout.rstrip('\f'), 'pdftotext-layout'
        raise RuntimeError(cp.stderr.strip()[-300:])
    raise RuntimeError('alternate_reader_unavailable: no local PDFium, ready isolated runtime, or pdftotext')


def _ocr(path: Path, index: int, page=None) -> dict:
    import ocr_runtime
    if ocr_runtime.probe().get("ready"):
        return ocr_runtime.recognize_pdf_page(path, index)
    raise RuntimeError(ocr_runtime.probe().get("setup_command") or "请运行 ocr_runtime.py prepare，然后更新知识库以重试")


def _ocr_text(result: dict) -> str:
    lines = result.get("lines") or []
    if not lines or any(not line.get("box") for line in lines):
        return str(result.get("text") or "").strip()
    # Keep cells on the same visual row together. This does not assert that any
    # arbitrary multi-column document has been semantically reconstructed.
    positioned = []
    for line in lines:
        box = line["box"]
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        positioned.append((sum(ys)/len(ys), min(xs), max(ys)-min(ys), line["text"]))
    rows = []
    for y, x, height, text in sorted(positioned):
        if rows and abs(y - rows[-1][0]) <= max(2, min(height, rows[-1][1]) * .5):
            rows[-1][2].append((x, text))
        else:
            rows.append([y, height, [(x, text)]])
    return "\n".join("\t".join(text for _, text in sorted(row)) for _, _, row in rows)


def _page(number: int, text: str, method: str, mapping: dict | None = None, attempts=None, ocr=None, native_text="", decoration_context=None) -> dict:
    q = quality(text, (mapping or {}).get("symbol_font_codepoints", ()))
    warnings = list((ocr or {}).get("warnings") or [])
    if mapping:
        q["font_coverage"] = mapping
        warnings += mapping.get("warnings", [])
        if mapping.get("warnings") and ocr is None:
            q["reasons"].append("font_inspection_incomplete")
        if mapping.get("invalid_mapping") and ocr is None:
            q["reasons"].append("incomplete_font_unicode_mapping")
    if ocr is not None:
        scores = [float(line["confidence"]) for line in ocr.get("lines", []) if line.get("confidence") is not None]
        q["ocr_min_confidence"] = min(scores) if scores else None
        q["ocr_mean_confidence"] = sum(scores)/len(scores) if scores else None
        exemptions = score_exemptions(ocr, native_text, mapping, decoration_context)
        content_scores = [float(line["confidence"]) for i, line in enumerate(ocr.get("lines", []))
                          if i not in exemptions and line.get("confidence") is not None]
        q["ocr_content_min_confidence"] = min(content_scores) if content_scores else None
        q["ocr_content_mean_confidence"] = sum(content_scores)/len(content_scores) if content_scores else None
        q["ocr_score_exemptions"] = [{"line_index": i, **reason} for i, reason in exemptions.items()]
        reference_diagnostic = {}
        corroborations = reference_corroborations(ocr, native_text, mapping, decoration_context, reference_diagnostic)
        q["ocr_reference_diagnostics"] = reference_diagnostic
        q["ocr_reference_corroborations"] = [{"line_index": i, **reason} for i, reason in corroborations.items()]
        # Corroborated references retain their low original/content OCR scores;
        # do not call them decorative or pretend to have character-level scores.
        unresolved_scores = [float(line["confidence"]) for i, line in enumerate(ocr.get("lines", []))
                             if i not in exemptions and i not in corroborations and line.get("confidence") is not None]
        if unresolved_scores and (min(unresolved_scores) < .80 or sum(unresolved_scores)/len(unresolved_scores) < .92):
            q["reasons"].append("low_ocr_confidence")
        q["ocr_quality_status"] = "native_corroborated" if corroborations else "ocr_only"
        q["ocr_numeric_accuracy_verified"] = False
        if corroborations:
            warnings.append("目录编号OCR原分数仍低；仅因原文本层编号、原始字符坐标和同排标题一致而可读，不代表数字OCR已准确或已人工核验")
        if exemptions:
            warnings.append("仅有原文本层和同排正文佐证的纯项目符号/目录点线不计入OCR置信门槛；原始识别行完整保留")
        warnings.append("OCR提取不等于逐字核验；数字、否定条件、手写与表格回看原件")
    q["readable"] = not q["reasons"]
    return {"page": number, "text": text.strip(), "method": method,
            "quality": q, "status": "usable" if q["readable"] else "needs_review",
            "attempts": attempts or [], "warnings": sorted(set(warnings)),
            **({"ocr_model": ocr.get("model_id"), "ocr_lines": ocr.get("lines") or [], "ocr_coordinates": ocr.get("coordinates") or {}} if ocr else {})}


def _finish(pages: list[dict], method: str) -> tuple[str, str, dict]:
    unresolved = [p["page"] for p in pages if p["status"] != "usable"]
    blank = [p["page"] for p in pages if not p["text"].strip()]
    text = "\n\n".join(f"### 第{p['page']}页\n{p['text']}" for p in pages)
    details = []
    offset = 0
    for p in pages:
        header = f"### 第{p['page']}页\n"
        start = offset + len(header)
        end = start + len(p["text"])
        details.append({k:v for k,v in p.items() if k != "text"} | {"text_start":start, "text_end":end, "text_sha256":hashlib.sha256(p["text"].encode()).hexdigest()})
        offset = end + 2
    meta = {"extraction_version": EXTRACTION_VERSION, "pages": len(pages), "pages_with_text": len(pages)-len(blank),
            "blank_pages": blank, "unresolved_pages": unresolved, "complete_text_coverage": bool(pages) and not unresolved,
            "semantic_accuracy_verified": False, "methods": dict(Counter(p["method"] for p in pages)),
            "pages_detail": details, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "scanned_ocr": any("ocr" in p["method"].lower() for p in pages),
            "image_text_coverage": {"status_counts": dict(Counter(p.get("image_text_coverage", {}).get("status", "not_evaluated") for p in pages)),
                                    "word_coverage_verified": False,
                                    "meaning": "OCR质量门槛与逐字覆盖核验不同；含图页面须查看状态和警告"}}
    return text, method, meta


def extract_pdf(path: Path) -> tuple[str, str, dict]:
    try:
        import pypdf
    except ImportError:
        raise RuntimeError("PDF质量预检需要pypdf：用当前Python安装 references/pdf-requirements.txt；OCR另运行 ocr_runtime.py prepare")
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            raise RuntimeError("PDF已加密，请提供已解密且获准使用的副本")
        if not 0 < len(reader.pages) <= MAX_PAGES:
            raise RuntimeError(f"PDF页数为空或超过{MAX_PAGES}页处理上限")
    except Exception as exc:
        _raise_if_uncontained(exc)
        if isinstance(exc, RuntimeError):
            raise
        # An independent parser can locate pages when pypdf cannot. Without
        # inspectable font mappings use explicit local OCR, never unchecked text.
        try:
            try:
                import pypdfium2 as pdfium
            except ImportError:
                import ocr_runtime
                count = ocr_runtime.read_native_pdf(path, mode='info')['pages']
            else:
                with pdfium.PdfDocument(str(path)) as alternate:
                    count = len(alternate)
            if type(count) is not int or not 0 < count <= MAX_PAGES:
                raise RuntimeError("PDF页数为空或超限")
            recovered = []
            for index in range(count):
                try:
                    result = _ocr(path, index)
                    row = _page(index+1, _ocr_text(result), str(result.get("method") or "rapidocr-cpu"), ocr=result)
                except Exception as error:
                    _raise_if_uncontained(error)
                    row = _page(index+1, "", "unresolved", attempts=[{"method":"ocr", "error":str(error)[:500]}])
                row["image_text_coverage"] = {"status": "ocr_quality_passed" if row["status"] == "usable" else "unverified",
                                               "coverage_verified": False, "semantic_accuracy_verified": False,
                                               "reason": "page_structure_uninspectable"}
                row["attempts"].insert(0, {"method":"pypdf-structure", "error":str(exc)[:300]})
                recovered.append(row)
            return _finish(recovered, "备用PDF结构解析／按需OCR")
        except Exception as alternate_error:
            _raise_if_uncontained(alternate_error)
            raise RuntimeError(f"PDF结构解析失败：{exc}；备用阅读器：{alternate_error}") from exc
    pages = []
    for index, page in enumerate(reader.pages):
        attempts = []
        mapping = font_coverage(page, reader)
        images = image_coverage(page, reader)
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception as exc:
            _raise_if_uncontained(exc)
            text = ""; attempts.append({"method":"pypdf-layout", "error":str(exc)[:300]})
        current = _page(index+1, text, "pypdf-layout", mapping)
        # Text quantity and image area cannot establish whether a bitmap has
        # facts. Recheck even dense OCR layers and small images conservatively.
        native_text = current["text"] if current["status"] == "usable" else ""
        # Coordinates from the OCR worker refer to an unrotated, uncropped
        # rendered page. Any ambiguity disables decoration score exceptions.
        decoration_context = None
        if (not images.get("warning") and not page.rotation
                and tuple(page.mediabox) == tuple(page.cropbox)
                and float(page.mediabox.left) == 0 and float(page.mediabox.bottom) == 0):
            decoration_context = {"width": float(page.mediabox.width), "height": float(page.mediabox.height),
                                  "bitmap_rectangles_pt": images.get("image_rectangles_pt", [])}
        raster_gap = bool(images.get("image_count") or images.get("warning"))
        image_text = {"status": "unverified" if raster_gap else "no_bitmap_detected",
                      "coverage_verified": not raster_gap,
                      "semantic_accuracy_verified": False}
        if raster_gap:
            current["quality"]["reasons"].append("image_text_coverage_unverified")
            # Retain the old diagnostic for compatibility, not as the gate.
            if images["total_image_fraction"] >= .45 and current["quality"]["characters"] < 200:
                current["quality"]["reasons"].append("dominant_image_sparse_text")
            current["quality"]["readable"] = False
            current["status"] = "needs_review"
        if current["status"] != "usable":
            attempts.append({"method":"pypdf-layout", "reasons":current["quality"]["reasons"]})
            try:
                alternative, method = _alternate(path, index)
                candidate = _page(index+1, alternative, method, mapping)
                attempts.append({"method":method, "reasons":candidate["quality"]["reasons"]})
                if candidate["status"] == "usable" and not raster_gap:
                    current = candidate
            except Exception as exc:
                _raise_if_uncontained(exc)
                attempts.append({"method":"alternate-text", "error":str(exc)[:300]})
        if current["status"] != "usable":
            try:
                result = _ocr(path, index, page)
                if native_text and decoration_context is not None and needs_reference_geometry(result, native_text):
                    try:
                        decoration_context["native_reference_geometry"] = load_reference_geometry(path, index)
                    except Exception as exc:
                        _raise_if_uncontained(exc)
                        attempts.append({"method": "native-reference-geometry", "error": str(exc)[:300]})
                current = _page(index+1, _ocr_text(result), str(result.get("method") or "rapidocr-cpu"), mapping, ocr=result, native_text=native_text, decoration_context=decoration_context)
                if raster_gap:
                    # OCR execution and its quality gate are observable; full
                    # semantic/word coverage is not certified by confidence.
                    image_text.update(status=("native_corroborated" if current["quality"].get("ocr_reference_corroborations")
                                              else "ocr_quality_passed") if current["status"] == "usable" else "ocr_needs_review",
                                      coverage_verified=False,
                                      method=current["method"])
                    if current["status"] == "usable" and native_text:
                        current["text"] = native_text + "\n\n[同页OCR补充，可能与可选文本重复；须回看原件]\n" + current["text"]
                        current["selectable_text_preserved"] = True
            except Exception as exc:
                _raise_if_uncontained(exc)
                attempts.append({"method":"ocr", "error":str(exc)[:500]})
                # Keep dubious text in diagnostics only, never in the returned
                # searchable body; a partial document cannot become evidence.
                current["text"] = ""
        current["attempts"] = attempts
        current["image_coverage"] = images
        current["image_text_coverage"] = image_text
        if raster_gap:
            current["warnings"].append("图像文字未经逐字覆盖核验，仍可能有遗漏；OCR质量通过也不等于全文准确，关键事实须回看原件")
        if current["status"] != "usable":
            current["visual_candidates"] = {"native": native_text,
                "ocr": _ocr_text({"lines": current.get("ocr_lines") or []}) if current.get("ocr_lines") else ""}
        pages.append(current)
    return _finish(pages, "按页PDF文本／按需OCR")


def extract_image(path: Path) -> tuple[str, str, dict]:
    import ocr_runtime
    result = ocr_runtime.recognize_image(path)
    row = _page(1, _ocr_text(result), str(result.get("method") or "rapidocr-cpu"), ocr=result)
    row["image_text_coverage"] = {"status": "ocr_quality_passed" if row["status"] == "usable" else "ocr_needs_review",
                                   "coverage_verified": False, "semantic_accuracy_verified": False}
    return _finish([row], "本地图片OCR")
