#!/usr/bin/env python3
"""Create a confirmed privacy scope for the local agent knowledge-base skill."""
from __future__ import annotations
import argparse, json, os, tempfile
from pathlib import Path


def fail(message: str, code: int = 2) -> None:
    print(json.dumps({"schema": "llm-wiki.scope-error.v1", "error": message}, ensure_ascii=False))
    raise SystemExit(code)


def canonical(value: str, *, must_exist: bool) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        fail(f"cannot resolve path {value!r}: {exc}")


def inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


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


parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
parser.add_argument("--source-root", required=True)
parser.add_argument("--vault", required=True)
parser.add_argument("--forbid", action="append", default=[])
parser.add_argument("--model-context-egress-approved", choices=("yes", "no"), required=True)
parser.add_argument("--mode", choices=("query-only", "query-and-ingest"), default="query-and-ingest")
parser.add_argument("--max-files", type=int, default=20)
parser.add_argument("--max-total-bytes", type=int, default=50 * 1024 * 1024)
parser.add_argument("--max-file-bytes", type=int, default=20 * 1024 * 1024)
parser.add_argument("--max-pdf-pages", type=int, default=120)
parser.add_argument("--max-extracted-chars", type=int, default=250_000)
parser.add_argument("--retention", default="keep until the user removes the vault")
parser.add_argument("--confirmation", required=True)
args = parser.parse_args()

if not 1 <= args.max_files <= 200:
    fail("max-files must be between 1 and 200")
if not 1024 <= args.max_file_bytes <= 64 * 1024 * 1024:
    fail("max-file-bytes must be between 1 KiB and 64 MiB")
if not args.max_file_bytes <= args.max_total_bytes <= 128 * 1024 * 1024:
    fail("max-total-bytes must be at least max-file-bytes and at most 128 MiB")
if not 1 <= args.max_pdf_pages <= 500:
    fail("max-pdf-pages must be between 1 and 500")
if not 10_000 <= args.max_extracted_chars <= 2_000_000:
    fail("max-extracted-chars must be between 10,000 and 2,000,000")
if len(args.confirmation.strip()) < 8:
    fail("confirmation must record the user's explicit authorization")

source_root = canonical(args.source_root, must_exist=True)
if not source_root.is_dir():
    fail("source-root must be an existing directory")
vault = canonical(args.vault, must_exist=False)
forbidden = [canonical(value, must_exist=False) for value in args.forbid]
if inside(source_root, forbidden):
    fail("source-root conflicts with forbidden_roots")
if inside(vault, forbidden):
    fail("vault conflicts with forbidden_roots")
if source_root == vault or source_root in vault.parents or vault in source_root.parents:
    fail("source-root and vault must not overlap in this first-run workflow")

output = canonical(args.output, must_exist=False)
scope = {
    "schema": "llm-wiki.privacy-scope.v2",
    "allowed_source_roots": [str(source_root)],
    "allowed_vaults": [str(vault)],
    "forbidden_roots": [str(path) for path in forbidden],
    "source_fetch_egress_approved": False,
    "model_context_egress_approved": args.model_context_egress_approved == "yes",
    "mode": args.mode,
    "budgets": {
        "max_files": args.max_files,
        "max_total_bytes": args.max_total_bytes,
        "max_file_bytes": args.max_file_bytes,
        "max_pdf_pages": args.max_pdf_pages,
        "max_extracted_chars_per_file": args.max_extracted_chars,
    },
    "retention": args.retention,
    "confirmation": args.confirmation.strip(),
}
atomic_json(output, scope)
print(json.dumps({
    "schema": "llm-wiki.scope-created.v1",
    "scope": str(output),
    "source_root": str(source_root),
    "vault": str(vault),
    "forbidden_root_count": len(forbidden),
    "model_context_egress_approved": scope["model_context_egress_approved"],
    "mode": scope["mode"],
    "budgets": scope["budgets"],
}, ensure_ascii=False, indent=2))
