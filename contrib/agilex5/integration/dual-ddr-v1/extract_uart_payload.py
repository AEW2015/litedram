#!/usr/bin/env python3
"""Strip only the known JTAG UART terminal banner from a bank-prefixed log."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

BANNER = (
    "juart-terminal: connected to hardware target using JTAG UART on cable",
    'juart-terminal: "Agilex_5E MDK Carrier [1-4.2.1]", device 1, instance 0',
    "juart-terminal: (Use the IDE stop button or Ctrl-C to terminate)",
)
BANK_LINE = re.compile(r"^BANK=[01]\s+\S.*$")


class ExtractionError(ValueError):
    pass


def split_lines(raw: bytes) -> list[tuple[bytes, str]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionError("UART log is not valid UTF-8") from exc
    if not raw or not (raw.endswith(b"\n")):
        raise ExtractionError("UART log is empty or ends with an incomplete line")
    result = []
    for chunk in text.splitlines(keepends=True):
        if chunk.endswith("\r\n"):
            body, ending = chunk[:-2], "\r\n"
        elif chunk.endswith("\n"):
            body, ending = chunk[:-1], "\n"
        else:
            raise ExtractionError("UART log contains a truncated line")
        result.append((body.encode("utf-8"), ending))
    return result


def extract(raw: bytes) -> tuple[bytes, dict]:
    lines = split_lines(raw)
    if len(lines) < len(BANNER):
        raise ExtractionError("missing standard three-line JTAG UART banner")
    for index, expected in enumerate(BANNER):
        if lines[index][0].decode("utf-8") != expected:
            raise ExtractionError(f"unexpected UART banner line {index + 1}")
    removed = len(BANNER)
    if len(lines) > removed and lines[removed][0] == b"":
        removed += 1

    payload_lines = lines[removed:]
    if not payload_lines:
        raise ExtractionError("UART log contains no bank-prefixed payload")
    for line_number, (body_bytes, _ending) in enumerate(payload_lines, 1):
        body = body_bytes.decode("utf-8")
        if not body:
            raise ExtractionError(f"payload line {line_number}: blank line")
        if not BANK_LINE.fullmatch(body):
            raise ExtractionError(f"payload line {line_number}: expected a complete BANK=0/1 line")

    payload = b"".join(body + ending.encode("ascii") for body, ending in payload_lines)
    metadata = {
        "removed_banner_lines": 3,
        "removed_optional_blank_line": removed == 4,
        "payload_lines": len(payload_lines),
        "input_bytes": len(raw),
        "payload_bytes": len(payload),
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    return payload, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_log", type=Path, help="unaltered juart-terminal capture")
    parser.add_argument("--output", required=True, type=Path, help="new payload file; existing files are preserved")
    parser.add_argument("--provenance", required=True, type=Path, help="new provenance JSON file; existing files are preserved")
    args = parser.parse_args()
    if args.output.exists() or args.provenance.exists():
        parser.error("refusing to overwrite an existing payload or provenance file")
    created_payload = False
    try:
        raw = args.raw_log.read_bytes()
        payload, metadata = extract(raw)
        metadata.update({"raw_log": str(args.raw_log.resolve()), "payload_file": str(args.output.resolve())})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.provenance.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as output_file:
            output_file.write(payload)
        created_payload = True
        with args.provenance.open("x", encoding="utf-8") as provenance_file:
            json.dump(metadata, provenance_file, indent=2, sort_keys=True)
            provenance_file.write("\n")
    except (OSError, ExtractionError) as exc:
        if created_payload:
            args.output.unlink(missing_ok=True)
        print(f"UART payload extraction rejected: {exc}", file=sys.stderr)
        return 2
    print(f"extracted bank-prefixed payload: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
