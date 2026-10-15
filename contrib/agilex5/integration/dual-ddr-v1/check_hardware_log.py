#!/usr/bin/env python3
"""Fail-closed validator for the dual DDR hardware diagnostic UART log."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

BANK_RE = re.compile(r"^BANK=([01])(?:\s|$)")
FIELD_RE = re.compile(r"(?<!\S)([A-Za-z][A-Za-z0-9_]*)=([^\s]+)")
WORD_BYTES = 32
DDR_CLOCK_HZ = 333_333_333.333333
DMA_WORDS = 2_097_152


class ValidationError(ValueError):
    pass


def fields(line: str) -> dict[str, str]:
    return dict(FIELD_RE.findall(line))


def integer(value: str, name: str, line_no: int) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValidationError(f"line {line_no}: invalid {name}={value!r}") from exc


def require_fields(parsed: dict[str, str], keys: tuple[str, ...], line_no: int) -> None:
    missing = [key for key in keys if key not in parsed]
    if missing:
        raise ValidationError(f"line {line_no}: missing fields {', '.join(missing)}")


def validate(text: str, log_sha256: str) -> dict:
    lines = text.splitlines()
    if not lines:
        raise ValidationError("empty log")

    events: dict[tuple, dict] = {}
    counts = Counter()
    per_bank = {0: defaultdict(int), 1: defaultdict(int)}
    dma_cycles = {0: {"normal": [], "full": []}, 1: {"normal": [], "full": []}}
    dma_seen: Counter = Counter()
    overlap_seen: Counter = Counter()
    case_seen: Counter = Counter()
    current_case = None
    global_markers = Counter()
    bank_diag = Counter()
    sparse_diag = Counter()
    final_diag = []
    training_selected = Counter()
    memory_passed = Counter()
    calibration_open = Counter()
    calibration_selected = Counter()
    ramp_passed = Counter()
    expected_search_failures = Counter()
    search_failures_by_bank = {0: Counter(), 1: Counter()}

    def unique(key: tuple, line_no: int, record: dict) -> None:
        if key in events:
            raise ValidationError(f"line {line_no}: duplicate phase {key}")
        events[key] = record

    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            raise ValidationError(f"line {line_no}: blank/incomplete log line")
        match = BANK_RE.match(line)
        if not match:
            raise ValidationError(f"line {line_no}: missing BANK=0/1 prefix")
        bank = int(match.group(1))
        body = line[match.end():]
        if re.search(r"TIMEOUT", body, re.IGNORECASE):
            raise ValidationError(f"line {line_no}: failure/mismatch/timeout marker")
        f = fields(body)
        counts["log_lines"] += 1

        if "PRIM_TRAIN_MISMATCH" in body:
            if training_selected[bank] or memory_passed[bank]:
                raise ValidationError(f"line {line_no}: primitive training mismatch after selected pass")
            expected_search_failures["primitive_training_mismatches"] += 1
            search_failures_by_bank[bank]["primitive_training_mismatches"] += 1
            continue
        if "DMA_CAL_READ_TRIAL" in body:
            require_fields(f, ("a5", "frame"), line_no)
            if not memory_passed[bank] or calibration_selected[bank] or ramp_passed[bank]:
                raise ValidationError(f"line {line_no}: calibration trial outside pre-selection search phase")
            calibration_open[bank] = 1
            counts["calibration_read_trials"] += 1
            continue
        if "DMA_CAL_SELECTED" in body:
            require_fields(f, ("a5", "frame", "write_a5"), line_no)
            if not memory_passed[bank] or not calibration_open[bank] or calibration_selected[bank] or ramp_passed[bank]:
                raise ValidationError(f"line {line_no}: unexpected or duplicate calibration selection")
            calibration_open[bank] = 0
            calibration_selected[bank] = 1
            counts["calibration_selections"] += 1
            continue
        if any(marker in body for marker in ("DMA_CAL_FAIL", "DMA_BAD", "DMA_CPU_BAD")):
            if not memory_passed[bank] or not calibration_open[bank] or calibration_selected[bank] or ramp_passed[bank]:
                raise ValidationError(f"line {line_no}: DMA search failure outside calibration trial window")
            marker = next(marker for marker in ("DMA_CAL_FAIL", "DMA_CPU_BAD", "DMA_BAD") if marker in body)
            expected_search_failures[marker.lower()] += 1
            search_failures_by_bank[bank][marker.lower()] += 1
            continue
        if "DMA_CAL_PASS" in body:
            require_fields(f, ("mode", "words", "issued", "moved", "errors"), line_no)
            if (not calibration_open[bank] or calibration_selected[bank] or f["mode"] not in ("R", "W")
                    or any(integer(f[key], key, line_no) != 128 for key in ("words", "issued", "moved"))
                    or integer(f["errors"], "errors", line_no) != 0):
                raise ValidationError(f"line {line_no}: calibration pass outside trial or with bad counters")
            counts["calibration_passes"] += 1
            continue
        if "DMA_CAL_WRITE_RESULT" in body:
            require_fields(f, ("read_a5", "write_a5", "frame", "count", "cpu", "dma_readback"), line_no)
            if not calibration_open[bank] or calibration_selected[bank]:
                raise ValidationError(f"line {line_no}: calibration write result outside trial window")
            counts["calibration_write_results"] += 1
            continue
        if "MISMATCH" in body or "FAIL" in body:
            raise ValidationError(f"line {line_no}: failure/mismatch marker")

        if "PRIMITIVE_DDR_TRAIN_PASS" in body:
            require_fields(f, ("w", "r", "a5"), line_no)
            unique(("train", bank), line_no, f)
            training_selected[bank] = 1
            counts["training_passes"] += 1
        elif "PUBLIC_LITEDRAM_PATTERN" in body:
            require_fields(f, ("mode", "bytes", "errors"), line_no)
            mode = integer(f["mode"], "mode", line_no)
            if mode not in range(6):
                raise ValidationError(f"line {line_no}: unexpected pattern mode {mode}")
            if integer(f["bytes"], "bytes", line_no) != 1_048_576 or integer(f["errors"], "errors", line_no) != 0:
                raise ValidationError(f"line {line_no}: pattern count/bytes/errors mismatch")
            unique(("pattern", bank, mode), line_no, f)
            per_bank[bank]["pattern_bytes"] += 1_048_576
            counts["pattern_passes"] += 1
        elif "PUBLIC_LITEDRAM_SPARSE" in body:
            require_fields(f, ("locations", "range_GiB", "errors"), line_no)
            if (integer(f["locations"], "locations", line_no) != 30 or
                    integer(f["range_GiB"], "range_GiB", line_no) != 8 or
                    integer(f["errors"], "errors", line_no) != 0):
                raise ValidationError(f"line {line_no}: sparse-test count/range/errors mismatch")
            unique(("sparse", bank), line_no, f)
            sparse_diag[bank] += 1
            per_bank[bank]["sparse_locations"] = 30
            counts["sparse_passes"] += 1
        elif "PUBLIC_LITEDRAM_MEM_PASS" in body:
            require_fields(f, ("bytes", "errors"), line_no)
            if integer(f["bytes"], "bytes", line_no) != 6_291_456 or integer(f["errors"], "errors", line_no) != 0:
                raise ValidationError(f"line {line_no}: aggregate memory count/errors mismatch")
            unique(("memory", bank), line_no, f)
            memory_passed[bank] = 1
            per_bank[bank]["memory_bytes"] = 6_291_456
            counts["memory_passes"] += 1
        elif "DMA_RAMP_PASS" in body:
            if not calibration_selected[bank] or ramp_passed[bank]:
                raise ValidationError(f"line {line_no}: DMA ramp pass without one prior calibration selection")
            unique(("ramp", bank), line_no, f)
            ramp_passed[bank] = 1
            counts["dma_ramp_passes"] += 1
        elif "DUAL_BANK_DIAGNOSTIC_PASS" in body:
            unique(("bankdiag", bank), line_no, f)
            bank_diag[bank] += 1
            counts["bank_diagnostics"] += 1
        elif "DUAL_SPARSE_PASS" in body:
            require_fields(f, ("locations", "range_GiB", "retention_ms", "errors"), line_no)
            if (integer(f["locations"], "locations", line_no) != 30 or
                    integer(f["range_GiB"], "range_GiB", line_no) != 8 or
                    integer(f["errors"], "errors", line_no) != 0):
                raise ValidationError(f"line {line_no}: dual sparse count/range/errors mismatch")
            unique(("dualsparse", bank), line_no, f)
            counts["dual_sparse_passes"] += 1
        elif "DUAL_FULL_DMA_CASE" in body or "DUAL_DMA_CASE" in body:
            full = "DUAL_FULL_DMA_CASE" in body
            require_fields(f, ("base", "words", "round"), line_no)
            try:
                base = int(f["base"], 16)
            except ValueError as exc:
                raise ValidationError(f"line {line_no}: invalid hexadecimal case base") from exc
            words = integer(f["words"], "words", line_no)
            round_id = integer(f["round"], "round", line_no)
            category = "full" if full else "normal"
            expected_words = 33_554_432 if full else DMA_WORDS
            if words != expected_words:
                raise ValidationError(f"line {line_no}: {category} DMA case length mismatch")
            expected_bases = (set(range(0, 8 * 0x02000000, 0x02000000)) if full
                              else {0x00200000, 0x00400000, 0x00600000})
            if base not in expected_bases or round_id != (base // 0x02000000 + 16 if full else base // 0x00200000 - 1):
                raise ValidationError(f"line {line_no}: unexpected {category} DMA case base/round")
            case_key = (category, base)
            if case_seen[case_key]:
                raise ValidationError(f"line {line_no}: duplicate DMA case {case_key}")
            case_seen[case_key] += 1
            current_case = case_key
            counts[f"{category}_dma_cases"] += 1
        elif "DUAL_FULL_DMA_OVERLAP" in body or "DUAL_DMA_OVERLAP" in body:
            require_fields(f, ("mode", "observed", "busy0", "busy1"), line_no)
            mode = f["mode"]
            if (mode not in ("W", "R") or integer(f["observed"], "observed", line_no) != 1
                    or integer(f["busy0"], "busy0", line_no) != 1
                    or integer(f["busy1"], "busy1", line_no) != 1):
                raise ValidationError(f"line {line_no}: invalid concurrent-overlap evidence")
            full = "DUAL_FULL_DMA_OVERLAP" in body
            category = "full" if full else "normal"
            if current_case is None or current_case[0] != category:
                raise ValidationError(f"line {line_no}: overlap does not belong to a matching DMA case")
            key = (current_case, mode)
            overlap_seen[key] += 1
            if overlap_seen[key] > 1:
                raise ValidationError(f"line {line_no}: duplicate overlap phase {key}")
            counts["overlap_observations"] += 1
        elif "DUAL_FULL_DMA_PASS" in body or "DUAL_DMA_PASS" in body:
            require_fields(f, ("mode", "words", "issued", "moved", "errors", "cycles", "read_cas", "write_cas"), line_no)
            mode = f["mode"]
            if mode not in ("W", "R"):
                raise ValidationError(f"line {line_no}: invalid DMA mode {mode}")
            vals = {key: integer(f[key], key, line_no) for key in ("words", "issued", "moved", "errors", "read_cas", "write_cas")}
            full = "DUAL_FULL_DMA_PASS" in body
            category = "full" if full else "normal"
            expected_words = 33_554_432 if full else DMA_WORDS
            if vals["words"] != expected_words or vals["issued"] != expected_words or vals["moved"] != expected_words or vals["errors"] != 0:
                raise ValidationError(f"line {line_no}: DMA word/error counter mismatch")
            expected = (expected_words, 0) if mode == "R" else (0, expected_words)
            if (vals["read_cas"], vals["write_cas"]) != expected:
                raise ValidationError(f"line {line_no}: DMA CAS counters mismatch")
            try:
                cycles = int(f["cycles"], 16)
            except ValueError as exc:
                raise ValidationError(f"line {line_no}: invalid hexadecimal cycles") from exc
            if cycles <= 0:
                raise ValidationError(f"line {line_no}: nonpositive DMA cycles")
            if current_case is None or current_case[0] != category:
                raise ValidationError(f"line {line_no}: DMA pass does not belong to a matching case")
            key = (current_case, bank, mode)
            if dma_seen[key]:
                raise ValidationError(f"line {line_no}: duplicate DMA result {key}")
            dma_seen[key] += 1
            dma_cycles[bank][category].append(cycles)
            per_bank[bank][f"{category}_dma_words"] += expected_words
            per_bank[bank][f"{category}_dma_bytes"] += expected_words * WORD_BYTES
            counts[f"{category}_dma_passes"] += 1
        elif "DUAL_DDR_DIAGNOSTIC_PASS" in body:
            require_fields(f, ("banks", "concurrent_cases"), line_no)
            if (integer(f["banks"], "banks", line_no) != 2 or integer(f["concurrent_cases"], "concurrent_cases", line_no) != 3
                    or integer(f.get("full_scan_GiB_per_bank", "0"), "full_scan_GiB_per_bank", line_no) != 8):
                raise ValidationError(f"line {line_no}: final diagnostic summary mismatch")
            final_diag.append((bank, line_no))
            counts["final_diagnostic_passes"] += 1
        elif "FAIL" in body:
            raise ValidationError(f"line {line_no}: failure marker")

    required = []
    expected_normal = {("normal", base) for base in (0x00200000, 0x00400000, 0x00600000)}
    expected_full = {("full", base) for base in range(0, 8 * 0x02000000, 0x02000000)}
    if set(case_seen) != expected_normal | expected_full:
        raise ValidationError("missing or unexpected normal/full DMA segment cases")
    for bank in (0, 1):
        required.extend([("train", bank), ("sparse", bank), ("memory", bank), ("ramp", bank), ("bankdiag", bank), ("dualsparse", bank)])
        required.extend(("pattern", bank, mode) for mode in range(6))
    missing = [key for key in required if key not in events]
    if missing:
        raise ValidationError(f"missing expected phases: {missing}")
    for bank in (0, 1):
        if bank_diag[bank] != 1 or sparse_diag[bank] != 1:
            raise ValidationError(f"bank {bank}: duplicate/missing bank or sparse diagnostic")
        if calibration_selected[bank] != 1 or ramp_passed[bank] != 1:
            raise ValidationError(f"bank {bank}: missing calibration selection or ramp pass")
        for category, cases in (("normal", expected_normal), ("full", expected_full)):
            for case in cases:
                for mode in ("W", "R"):
                    if dma_seen[(case, bank, mode)] != 1:
                        raise ValidationError(f"bank {bank}: missing DMA result for {case}/{mode}")
        for case in expected_normal | expected_full:
            for mode in ("W", "R"):
                if overlap_seen[(case, mode)] != 1:
                    raise ValidationError(f"missing overlap observation for {case}/{mode}")
    if len(final_diag) != 1 or final_diag[0][0] != 0 or final_diag[0][1] != len(lines):
        raise ValidationError("missing, duplicate, or misplaced final dual diagnostic pass")
    if counts["dual_sparse_passes"] != 2:
        raise ValidationError("expected one dual sparse summary per bank")

    for bank in (0, 1):
        for category in ("normal", "full"):
            cycles = sum(dma_cycles[bank][category])
            per_bank[bank][f"{category}_dma_cases"] = 3 if category == "normal" else 8
            per_bank[bank][f"{category}_dma_cycles_total"] = cycles
            per_bank[bank][f"{category}_bandwidth_bytes_per_second_estimate"] = round(
                per_bank[bank][f"{category}_dma_bytes"] * DDR_CLOCK_HZ / cycles)
            per_bank[bank]["bandwidth_clock_assumption_hz"] = DDR_CLOCK_HZ

    return {
        "result": "PASS",
        "qualification": "nominalBoardOnlyNotPVT",
        "log_sha256": log_sha256,
        "inputs_sha256": {"hardware_log": log_sha256,
                           "validator": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "log_bytes": len(text.encode()),
        "counts": dict(sorted(counts.items())),
        "expected_search_failures": dict(sorted(expected_search_failures.items())),
        "expected_search_failures_by_bank": {
            str(bank): dict(sorted(search_failures_by_bank[bank].items())) for bank in (0, 1)
        },
        "expected_search_failures_note": "These markers were confined to pre-selection primitive training or DMA calibration search; they are not post-selection pass evidence.",
        "per_bank": {str(bank): dict(sorted(per_bank[bank].items())) for bank in (0, 1)},
        "assumptions": ["bandwidth estimates use 333.333 MHz as the DMA cycle clock; confirm this clock-domain interpretation before comparing rates"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?", type=Path, help="UART log (default: stdin)")
    parser.add_argument("--output", required=True, type=Path, help="new JSON report path; existing files are preserved")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite existing output: {args.output}")
    try:
        raw = args.log.read_bytes() if args.log else sys.stdin.buffer.read()
        text = raw.decode("utf-8")
        report = validate(text, hashlib.sha256(raw).hexdigest())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as out:
            json.dump(report, out, indent=2, sort_keys=True)
            out.write("\n")
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        print(f"hardware log rejected: {exc}", file=sys.stderr)
        return 2
    print(f"validated dual-bank log; report={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
