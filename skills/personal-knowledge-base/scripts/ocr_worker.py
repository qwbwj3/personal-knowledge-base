#!/usr/bin/env python3
"""One local CPU OCR job, then exit. No implicit download or source upload."""
from __future__ import annotations
import argparse
import contextlib
import importlib.util
import json
import math
from pathlib import Path
import socket
import sys
import time

MAX_PIXELS = 8_000_000
MAX_SIDE = 4096


def deny_network(*args, **kwargs):
    raise RuntimeError('Network/download disabled during OCR; run ocr_runtime.py prepare explicitly')


def offline():
    # Defense in depth for Python HTTP clients, including future library changes.
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    socket.getaddrinfo = deny_network
    def audit(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:
            deny_network()
    sys.addaudithook(audit)


def load_facade():
    spec = importlib.util.spec_from_file_location('ocr_facade', Path(__file__).with_name('ocr_runtime.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def engine(home, facade):
    facade._check_versions(home)
    facade.verify_models(home)
    from rapidocr import RapidOCR, OCRVersion, ModelType
    from rapidocr.utils.download_file import DownloadFile
    DownloadFile.run = staticmethod(deny_network)
    params = {
        'Global.log_level': 'error', 'Global.text_score': 0.0,
        'Global.max_side_len': MAX_SIDE,
        'Global.model_root_dir': str(home / 'models'),
        'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        'EngineConfig.onnxruntime.enable_cpu_mem_arena': False,
        'EngineConfig.onnxruntime.use_cuda': False,
        'EngineConfig.onnxruntime.use_dml': False,
        'EngineConfig.onnxruntime.use_coreml': False,
        'EngineConfig.onnxruntime.use_cann': False,
        'Det.limit_side_len': 1536, 'Det.limit_type': 'max',
        'Rec.rec_batch_num': 2,
    }
    for stage, item in facade._spec()['models'].items():
        params[f'{stage.title()}.model_path'] = str(home / 'models' / item['file'])
    for stage in ('Det', 'Rec'):
        params[f'{stage}.ocr_version'] = OCRVersion.PPOCRV6
        params[f'{stage}.model_type'] = ModelType.SMALL
    result = RapidOCR(params=params)
    for session in (result.text_det.session, result.text_rec.session, result.text_cls.session):
        if session.session.get_providers() != ['CPUExecutionProvider']:
            raise RuntimeError('Unexpected non-CPU execution provider')
    return result


def bounded_scale(width, height):
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise RuntimeError('Invalid image/page dimensions')
    return min(1.0, math.sqrt(MAX_PIXELS / (width * height)), MAX_SIDE / max(width, height))


def load_pdf(path, index):
    import pypdfium2 as pdfium
    import numpy as np
    with pdfium.PdfDocument(path) as doc:
        if not 0 <= index < len(doc):
            raise RuntimeError(f'Page index {index} out of range (pages={len(doc)})')
        with contextlib.closing(doc[index]) as page:
            width, height = page.get_size()
            scale = (200 / 72) * bounded_scale(width * 200 / 72, height * 200 / 72)
            # PDFium rounds up each dimension; retain a one-pixel safety margin.
            while math.ceil(width * scale) * math.ceil(height * scale) > MAX_PIXELS:
                scale *= 0.999
            bitmap = page.render(scale=scale)
            try:
                pixels = np.array(bitmap.to_pil().convert('RGB'))[:, :, ::-1].copy()
            finally:
                bitmap.close()
    return pixels, {'coordinate_space': 'rendered_page_pixels', 'render_scale': scale,
                    'page_index': index, 'width': pixels.shape[1], 'height': pixels.shape[0]}


def load_image(path):
    import numpy as np
    from PIL import Image, ImageOps
    # Reject bombs before decoding; ordinary oversized images are explicitly rejected,
    # rather than decoding huge buffers then pretending the pixel cap protected memory.
    with Image.open(path) as source:
        if source.width * source.height > MAX_PIXELS or max(source.size) > MAX_SIDE:
            raise RuntimeError(f'Image exceeds {MAX_PIXELS} pixels or {MAX_SIDE}px side; resize locally first')
        image = ImageOps.exif_transpose(source).convert('RGB')
        pixels = np.array(image)[:, :, ::-1].copy()
    return pixels, {'coordinate_space': 'exif_oriented_image_pixels',
                    'width': pixels.shape[1], 'height': pixels.shape[0]}


def self_check(ocr):
    """Exercise real detection and recognition with Pillow's bundled font only."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    expected = 'OCR PROBE 123'
    sample = Image.new('RGB', (640, 160), 'white')
    ImageDraw.Draw(sample).text((32, 40), expected, font=ImageFont.load_default(size=48), fill='black')
    result = ocr(np.array(sample)[:, :, ::-1].copy())
    raw = ' '.join(str(text) for text in result.txts) if result.txts is not None else ''
    scores = [float(score) for score in result.scores] if result.scores is not None else []
    # Only normalize whitespace, not letters/digits: no substitutions or fuzzy match.
    if ' '.join(raw.split()) != expected or not scores or any(not math.isfinite(s) or s < 0.8 for s in scores):
        raise RuntimeError(f'OCR self-check failed: expected {expected!r}, got {raw!r}, scores={scores}')
    return {'ready': True, 'text': raw, 'confidence': min(scores)}


def run(args):
    start = time.perf_counter()
    facade = load_facade()
    offline()
    if args.command.startswith('native-'):
        # PDFium is part of the pinned runtime. Do not load OCR models to read
        # native text/geometry, and never install a missing reader implicitly.
        facade._check_versions(args.home)
        if args.command == 'native-geometry':
            from ocr_decorations import _pdfium_reference_geometry
            return _pdfium_reference_geometry(args.path, args.page_index)
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(str(args.path)) as document:
            count = len(document)
            if not 0 < count <= 500:
                raise ValueError('Native PDF page count outside supported range')
            if args.command == 'native-info':
                return {'reader': 'pdfium-runtime', 'pages': count}
            if not 0 <= args.page_index < count:
                raise ValueError('Invalid native PDF page index')
            with contextlib.closing(document[args.page_index]) as page:
                with contextlib.closing(page.get_textpage()) as textpage:
                    if textpage.count_chars() > 200000:
                        raise ValueError('Native PDF page text limit exceeded')
                    return {'reader': 'pdfium-runtime', 'text': textpage.get_text_range()}
    ocr = engine(args.home, facade)
    if args.command == 'self-check':
        return self_check(ocr)
    pixels, coordinates = load_pdf(args.path, args.page_index) if args.command == 'pdf-page' else load_image(args.path)
    result = ocr(pixels)
    lines = []
    if result.txts is not None:
        for text, box, score in zip(result.txts, result.boxes, result.scores):
            lines.append({'text': str(text), 'box': [[float(x), float(y)] for x, y in box],
                          'confidence': float(score)})
    warnings = []
    if not lines:
        warnings.append('No text detected; blank page or OCR failure requires visual review')
    low = sum(line['confidence'] < 0.85 for line in lines)
    if low:
        warnings.append(f'{low} line(s) below 0.85 confidence; verify against original')
    warnings.append('OCR is fallible even at high confidence; raw lines are not corrected or reconstructed as a table')
    output = dict(text='\n'.join(line['text'] for line in lines), lines=lines, warnings=warnings,
                  method='rapidocr-onnxruntime-cpu', model_id=facade._spec()['model_id'],
                  coordinates=coordinates, elapsed_seconds=round(time.perf_counter() - start, 3))
    from process_metrics import current_process_memory
    output.update(current_process_memory())
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('command', choices=['image', 'pdf-page', 'self-check', 'native-text', 'native-geometry', 'native-info'])
    parser.add_argument('path', type=Path, nargs='?')
    parser.add_argument('--page-index', type=int, default=0)
    args = parser.parse_args()
    try:
        # Keep stdout machine-readable even if a library prints diagnostics.
        with contextlib.redirect_stdout(sys.stderr):
            result = run(args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}\nSetup: python ocr_runtime.py prepare --home "{args.home}"', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
