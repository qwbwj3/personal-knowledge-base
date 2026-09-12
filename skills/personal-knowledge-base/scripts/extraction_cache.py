"""One read-only normalization boundary for verified extraction cache versions.

Callers must validate the release manifest and originals first. Legacy identity
comes from an unambiguous snapshot association, never from guessed source text.
"""
import hashlib


def normalize(document: dict, snapshots: list[dict]) -> dict:
    schema = document.get("schema")
    if schema not in ("personal-kb.extractions.v1", "personal-kb.extractions.v2"):
        raise ValueError("unknown_extraction_cache_schema")
    identities = {}
    for item in snapshots:
        if item.get("extraction_id"):
            identities.setdefault(item["extraction_id"], set()).add(item.get("sha256"))
    result = {}
    for key, original in document.get("sources", {}).items():
        value = dict(original)
        eid = value.get("extraction_id")
        if not eid or eid not in identities:
            continue  # Unreferenced cache is not a source available to readers.
        sources = identities[eid]
        if len(sources) != 1 or None in sources:
            raise ValueError("ambiguous_extraction_source")
        source = next(iter(sources))
        legacy = schema == "personal-kb.extractions.v1"
        if (legacy and key != source) or (not legacy and key != eid):
            raise ValueError("extraction_cache_key_mismatch")
        if value.get("source_sha256") is None and (legacy or value.get("chunk_format") == "legacy-paragraph-v1"):
            value["source_sha256"] = source
        if value.get("source_sha256") != source or hashlib.sha256(value["text"].encode()).hexdigest() != value.get("text_sha256"):
            raise ValueError("extraction_cache_identity_mismatch")
        if legacy:
            value["chunk_format"] = "legacy-paragraph-v1"
        if eid in result and result[eid] != value:
            raise ValueError("duplicate_extraction_identity")
        result[eid] = value
    return result
