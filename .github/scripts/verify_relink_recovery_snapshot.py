#!/usr/bin/env python3
"""Fail-closed semantic check for reusing a Linker recovery on a rebuilt DB.

The normal serial pipeline requires a byte-identical lines snapshot. A recovery
has to rebuild the DB, however, and Sefaria image embedding can legitimately
change an image ``src`` from its remote ``textimages.sefaria.org`` URL to an
inline data URI when a previously transient download succeeds. Such a change
cannot affect Linker output, but every other source change must still fail.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
from pathlib import Path


IMAGE_TAG = re.compile(
    r"(?P<prefix><img\s+[^>]*?\bsrc\s*=\s*)(?P<quote>[\"'])"
    r"(?P<src>[^\"']+)(?P=quote)(?P<suffix>[^>]*?/?>)",
    re.IGNORECASE,
)
TEXTIMAGE_PREFIX = "https://textimages.sefaria.org/"
DATA_IMAGE = re.compile(
    r"data:(image/(?:png|jpeg|gif|svg\+xml|webp));base64,"
    r"(?P<payload>[A-Za-z0-9+/]*={0,2})\Z",
    re.IGNORECASE,
)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PAYLOAD_META_SCHEMAS = frozenset({2, 3})
BASELINE_SCHEMA = 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _remote_inline_equivalent(src_a: str, src_b: str) -> bool:
    if src_a.startswith(TEXTIMAGE_PREFIX):
        remote, inline = src_a, src_b
    elif src_b.startswith(TEXTIMAGE_PREFIX):
        remote, inline = src_b, src_a
    else:
        return False
    if not remote[len(TEXTIMAGE_PREFIX) :] or any(c.isspace() for c in remote):
        return False
    match = DATA_IMAGE.fullmatch(inline)
    if match is None:
        return False
    try:
        decoded = base64.b64decode(match.group("payload"), validate=True)
    except ValueError:
        return False
    return 0 < len(decoded) <= MAX_IMAGE_BYTES


def _split_image_sources(value: str) -> tuple[list[str], list[str]]:
    """Return exact non-src spans and ordered img src values."""
    spans: list[str] = []
    sources: list[str] = []
    cursor = 0
    for match in IMAGE_TAG.finditer(value):
        spans.append(value[cursor : match.start("src")])
        sources.append(match.group("src"))
        cursor = match.end("src")
    spans.append(value[cursor:])
    return spans, sources


def _image_src_equivalent(left: str, right: str) -> bool:
    left_spans, left_sources = _split_image_sources(left)
    right_spans, right_sources = _split_image_sources(right)
    if not left_sources or left_spans != right_spans:
        return False
    return all(
        src_a == src_b or _remote_inline_equivalent(src_a, src_b)
        for src_a, src_b in zip(left_sources, right_sources, strict=True)
    )


def _schema(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE type IN ('table','index','view','trigger') ORDER BY type,name"
    ).fetchall()


def _meta(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT key,value FROM lines_snapshot_meta ORDER BY key"
    ).fetchall()


def verify(
    original: Path,
    rebuilt: Path,
    artifacts: Path,
    payload_meta: Path,
    baseline_manifest: Path,
) -> tuple[int, int]:
    meta = _json(payload_meta)
    baseline = _json(baseline_manifest)
    original_sha = _sha256(original)
    expected_sha = meta.get("snapshot", {}).get("sha256")
    if not isinstance(expected_sha, str) or original_sha != expected_sha:
        raise SystemExit("original raw snapshot SHA does not match Linker payload metadata")
    if baseline.get("snapshot_sha256") != original_sha:
        raise SystemExit("line baseline is not bound to the original raw snapshot")
    # The Linker bumped meta.json to schema 3 in a9ae2d4 (2026-08-31) without
    # changing ``snapshot.sha256`` — the only field read here. The line baseline
    # manifest is still schema 2 (line_baseline.SCHEMA_VERSION).
    if meta.get("schema_version") not in PAYLOAD_META_SCHEMAS:
        raise SystemExit(
            "recovery snapshot comparison requires Linker meta schema "
            f"{sorted(PAYLOAD_META_SCHEMAS)}, got {meta.get('schema_version')!r}"
        )
    if baseline.get("schema_version") != BASELINE_SCHEMA:
        raise SystemExit(
            f"recovery snapshot comparison requires line baseline schema {BASELINE_SCHEMA}, "
            f"got {baseline.get('schema_version')!r}"
        )

    connections = []
    for path in (original, rebuilt):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA mmap_size=4294967296")
        connection.execute("PRAGMA cache_size=-524288")
        connections.append(connection)
    old, new = connections
    if _schema(old) != _schema(new):
        raise SystemExit("rebuilt snapshot schema differs from the linked snapshot")
    if _meta(old) != _meta(new):
        raise SystemExit("rebuilt snapshot metadata differs from the linked snapshot")

    query = (
        "SELECT source_name,canonical_he_title,line_index,content,context_ref "
        "FROM lines_snapshot ORDER BY source_name,canonical_he_title,line_index"
    )
    old_rows, new_rows = old.execute(query), new.execute(query)
    safe_differences: set[tuple[str, str, int]] = set()
    count = 0
    while True:
        before, after = old_rows.fetchone(), new_rows.fetchone()
        if before is None or after is None:
            if before is not None or after is not None:
                raise SystemExit("rebuilt snapshot row count differs from linked snapshot")
            break
        count += 1
        if before[:3] != after[:3]:
            raise SystemExit(f"rebuilt snapshot line identity differs at row {count}")
        if before[4] != after[4]:
            raise SystemExit(f"rebuilt snapshot context_ref differs at {before[:3]!r}")
        if before[3] != after[3]:
            if not _image_src_equivalent(before[3], after[3]):
                raise SystemExit(
                    f"rebuilt snapshot text differs outside a safe image src at {before[:3]!r}"
                )
            safe_differences.add((before[0], before[1], before[2]))
            if len(safe_differences) > 10_000:
                raise SystemExit("too many image-src differences for a bounded recovery")

    if safe_differences:
        for artifact in artifacts.rglob("*.jsonl"):
            with artifact.open(encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    book = record.get("book_key", {})
                    key = (
                        book.get("source_name"),
                        book.get("canonical_he_title"),
                        record.get("line_index"),
                    )
                    if key in safe_differences:
                        raise SystemExit(
                            f"image-src changed line has a Linker record: {key!r} "
                            f"({artifact}:{number})"
                        )
    return count, len(safe_differences)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--rebuilt", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--payload-meta", required=True, type=Path)
    parser.add_argument("--baseline-manifest", required=True, type=Path)
    args = parser.parse_args()
    rows, differences = verify(
        args.original,
        args.rebuilt,
        args.artifacts,
        args.payload_meta,
        args.baseline_manifest,
    )
    print(
        f"RECOVERY_SNAPSHOT_SEMANTIC_OK rows={rows} "
        f"unlinked_image_src_differences={differences}"
    )


if __name__ == "__main__":
    main()
