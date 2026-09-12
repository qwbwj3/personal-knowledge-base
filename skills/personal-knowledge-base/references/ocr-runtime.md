# Local CPU OCR runtime

This optional adapter supplies OCR evidence to the knowledge-base quality gate. It does not decide whether OCR may be cited, alter source PDFs, reconstruct tables, correct characters, replace historical oracles, or reduce creation/product workflows.

## Public contract

`ocr_runtime.py` imports only Python standard-library modules. Its facade can be imported by an existing Python 3.9+ application; **prepare requires Python 3.12 or 3.13**, with 3.12 tested/recommended for this dependency lock.

- `probe() -> dict`: `ready`, `reason`, `setup_command`, `home`, `engine`, `engine_version`, `provider`, `model_id`, `integrity`. Reads installed distribution metadata, the prepared marker and model stats only. No subprocess/model loading/writes/network. It is a cheap readiness hint, not a full native-library or checksum test.
- `recognize_pdf_page(path: Path, page_index: int) -> dict`: **zero-based** index. Always renders that page, independent of any nonempty/garbled embedded text.
- `recognize_image(path: Path) -> dict`: local file only. EXIF orientation applied, no remote URL input.
- Both return `text`, `lines: [{text, box: [[x,y],...], confidence}]`, `warnings`, `method`, `model_id`. Additional `coordinates`, `elapsed_seconds` and (POSIX) `peak_rss_bytes` expose provenance/measurement. Raw line text and confidence are retained, including low-score lines (`Global.text_score=0.0`). No table/value correction.
- PDF boxes use top-left-origin **rendered-page pixels**, not PDF points. `coordinates.render_scale` gives pixels per PDF point; CLI page numbers are one-based. Image boxes use EXIF-oriented image pixels.
- Failures raise a readable `RuntimeError` with setup/repair command. Unready `check` exits 1; operational CLI failure exits 2. Successful CLI output is JSON.

## Explicit setup and usage

From the skill scripts directory (substitute the actual absolute script path when elsewhere):

```sh
python3.12 ocr_runtime.py prepare
python3.12 ocr_runtime.py check
python3.12 ocr_runtime.py pdf-page /absolute/document.pdf --page 1
python3.12 ocr_runtime.py image /absolute/image.png
```

Windows PowerShell, **native 64-bit Python**, no WSL/CUDA/NVIDIA needed:

```powershell
py -3.12 ocr_runtime.py prepare
py -3.12 ocr_runtime.py check
py -3.12 ocr_runtime.py pdf-page 'C:\Documents\文件.pdf' --page 1
```

`PERSONAL_KB_OCR_HOME` selects an isolated runtime. Every CLI subcommand also accepts `--home PATH`; it takes precedence for that invocation. Default locations are outside the skill/distribution:

- macOS: `~/Library/Application Support/personal-kb/ocr-runtime-v1`
- Windows: `%LOCALAPPDATA%\personal-kb\ocr-runtime-v1`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/personal-kb/ocr-runtime-v1`

Only `prepare` installs packages/downloads models. It creates a dedicated venv, installs exact direct/transitive versions from `ocr-requirements.lock`, checks dependency compatibility, downloads only official model URLs, checks SHA-256 and atomically replaces missing/corrupt models. Before publishing `ready.json`, an offline CPU self-check draws `OCR PROBE 123` using Pillow's bundled font and runs real detection/classification/recognition. It requires the exact text (whitespace normalization only) and finite line confidence >=0.8. The generated image stays in memory; no external font or network is used. This is an execution check, not a multilingual accuracy benchmark. Each prepare writes a new `generations/<uuid>` directory at its final path, including its own venv and models. The top-level marker first records `building`; only successful install, hashes and actual self-check publish `ready` with that generation. Failed/old generations are retained rather than deleted. A child installer left behind by an interrupted prepare cannot alter a later generation. Existing old-format ready markers remain readable; preparing again migrates to the generation format.

`prepare.lock` now uses a nonblocking operating-system file lock: `fcntl.flock` on POSIX, `msvcrt.locking` on Windows. A concurrent prepare fails before changing readiness or installing anything. Closing the handle or exiting the process releases the lock, including forced process termination. The file itself is intentionally retained and harmless; **do not delete it** (unlinking a live lock could create two separately locked files). Existing leftover files from the earlier existence-lock implementation no longer block acquisition. Do not run prepare concurrently with query workers. Do not relocate an existing venv: prepare at its final location instead. Lock semantics follow the [Python fcntl documentation](https://docs.python.org/3/library/fcntl.html) and [Python msvcrt documentation](https://docs.python.org/3/library/msvcrt.html); Windows permits the one-byte lock even past EOF, so initialization does not race to write a byte.

Prepare needs network access to PyPI and ModelScope, local disk space, and venv/SSL support. Never put models, venvs, originals, or private OCR evidence in the release Git tree. Package versions are locked, but wheel hashes are not a hermetic cross-platform supply-chain lock; antlr4's upstream sdist may use a build environment. Only model payloads have committed SHA-256 pins.

## Offline and resource behavior

Recognition launches one dedicated (`-B -X utf8`) venv Python process per page/image, preserving `PYTHONPATH` and normal host `sitecustomize` safeguards; no persistent model singleton. All model hashes are verified in the worker before loading. Explicit local model paths and the ONNX-embedded recognition dictionary avoid lazy downloads. The RapidOCR download function is disabled; Python socket/DNS calls are denied before importing the engine. This prevents implicit library networking, not a security sandbox for hostile native code. Original files are read locally and never uploaded.

Only `CPUExecutionProvider` is accepted. Two intra-op threads, one inter-op thread, batch size two, no CPU arena or GPU providers. PDFium renders at up to 200 dpi with an 8-million-pixel / 4096px-side cap; actual dimensions and scale are returned. Oversized image files are rejected before decoding; callers may resize them explicitly. PDF detection is bounded to 1536px longest side, while recognition crops use the rendered page. These are workload controls, **not a guaranteed total-memory ceiling**. Workers exit after each task; a 180-second timeout kills/waits for the subprocess.

The venv lock targets Python 3.12/3.13. Availability of packages and native libraries must be checked in the selected interpreter on the target host. Windows ARM64 and constrained-memory devices are not certified by this release. Some installations need Microsoft runtime libraries to load native wheels; prepare's smoke test reports native load errors instead of declaring readiness. High-confidence OCR may still be wrong. Original-page review remains necessary.

## Pinned model provenance

- [RapidOCR official parameter documentation](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/parameters/): PP-OCRv6 small requires RapidOCR >=3.9.0; enum-based parameters are used in the worker.
- [RapidOCR 3.9.2 PyPI release](https://pypi.org/project/rapidocr/3.9.2/): pinned package release.
- [v3.9.2 official model manifest](https://github.com/RapidAI/RapidOCR/blob/v3.9.2/python/rapidocr/default_models.yaml): exact URLs/hashes copied to `ocr-models.json`, downloaded payloads must match the manifest hashes.
- [v3.9.2 configuration](https://github.com/RapidAI/RapidOCR/blob/v3.9.2/python/rapidocr/config.yaml) and [initialization source](https://github.com/RapidAI/RapidOCR/blob/v3.9.2/python/rapidocr/main.py): classifier initializes even if inference classification is disabled. Therefore the small PP-OCRv4 orientation classifier is explicitly pinned alongside **PP-OCRv6 small det + rec**. Classification remains enabled. Models total about 30 MiB, with dictionary embedded in rec ONNX; no font download/visualization call.
- [ONNX Runtime 1.23.2](https://pypi.org/project/onnxruntime/1.23.2/) and [pypdfium2 5.0.0](https://pypi.org/project/pypdfium2/5.0.0/): pinned CPU/backend versions; target-host availability is checked during setup.

## Verification boundaries

The runtime must pass its generated-text self-check and checksum verification on the selected host before use. That test checks execution, not business-document accuracy, handwriting, table structure or total system-memory usage. Recognition errors must remain visible to the caller and can require the host visual-review workflow.

This distribution contains code and dependency/model manifests only. It does not include business PDFs, extracted passages, model payloads, generated self-check output, host configuration, credentials or local test reports. Prepare the runtime outside the Git checkout and the installed Skill. Do not upload OCR results or original pages to a repository when reporting an issue.
