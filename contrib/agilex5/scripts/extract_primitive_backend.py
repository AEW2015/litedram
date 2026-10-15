#!/usr/bin/env python3
"""Freeze generated PHY Lite RTL/QIP as a project-owned source snapshot.

This is a migration-stage extraction, not an independently designed PHY: the
snapshot retains Altera PHY Lite fabric mapping, primitive wrappers, and its
hierarchy-dependent constraints. It deliberately does not copy calibration IP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from collections import Counter


REPO = Path(__file__).resolve().parents[1]
SOURCES = {
    "1200": ("build/strobe-hybrid-1200-ref100", "strobe-hybrid-1200-ref100"),
    "1250": ("build/strobe-hybrid-1250-ref125", "strobe-hybrid-1250-ref125"),
    "1333": ("build/strobe-hybrid-1333-refauto-unsafe", "strobe-hybrid-1333-refauto-unsafe"),
}
PHY_REL = Path("ip/phy_probe/phy_probe_phy")

# These assignments describe how Platform Designer generated the snapshot.
# Keep all IO electrical, hierarchy placement, RTL, include, and timing files.
DROP_NAMES = {
    "IP_TOOL_NAME", "IP_TOOL_VERSION", "IP_TOOL_ENV", "IP_TOOL_VENDOR_NAME",
    "PRE_COMPILED_MODULE", "OCS_IP_FILE", "OCS_IP_TYPE", "OCS_IP_VERSION",
    "OCS_IP_HASH", "SOPCINFO_FILE", "SLD_INFO", "IP_TARGETED_DEVICE_FAMILY",
    "IP_TARGETED_PART_TRAIT", "IP_GENERATED_DEVICE_FAMILY", "IP_QSYS_MODE",
    "IP_TOP_LEVEL_COMPONENT_NAME", "IP_FILE", "QSYS_FILE",
    "IP_COMPONENT_NAME", "IP_COMPONENT_DISPLAY_NAME",
    "IP_COMPONENT_REPORT_HIERARCHY", "IP_COMPONENT_INTERNAL",
    "IP_COMPONENT_AUTHOR", "IP_COMPONENT_VERSION", "IP_COMPONENT_DESCRIPTION",
    "IP_COMPONENT_GROUP", "IP_COMPONENT_DOCUMENTATION_LINK",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inventory(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): sha256(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def primitive_inventory(root: Path) -> dict[str, int]:
    """Count explicit tennm_* atom instance sites by module type."""
    pattern = re.compile(r"\b(tennm_[A-Za-z0-9_]+)\s*(?:#\s*\(|[A-Za-z_][A-Za-z0-9_]*\s*\()")
    counts: Counter[str] = Counter()
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".sv", ".v"}:
            counts.update(pattern.findall(path.read_text(errors="ignore")))
    return dict(sorted(counts.items()))


def prune_qip(qip: Path) -> list[str]:
    old = qip.read_text()
    kept, removed = [], []
    for line in old.splitlines(keepends=True):
        match = re.search(r"(?:-name\s+|\s)([A-Z][A-Z0-9_]+)(?:\s|$)", line)
        # Assignment syntax puts the name after -name. The fallback supports
        # uncommon generated IP_FILE/QSYS_FILE lines without -name.
        name = match.group(1) if match else ""
        non_source_misc = ("MISC_FILE" in line and re.search(r"\.(?:ip|qsys|cmp|sopcinfo)(?:\"|\])", line, re.I))
        if name in DROP_NAMES or non_source_misc or re.search(r"\b-name\s+(?:IP|OCS_IP|QSYS)_", line):
            removed.append(line.rstrip("\r\n"))
        else:
            kept.append(line)
    qip.write_text("".join(kept))
    return removed


def check_qip(root: Path, qip: Path) -> list[str]:
    errors: list[str] = []
    text = qip.read_text()
    if re.search(r"\b(?:OCS_IP_FILE|IP_FILE|QSYS_FILE)\b|\.ip\b|\.qsys\b", text, re.I):
        errors.append("QIP still contains IP/Qsys regeneration references")
    for line in text.splitlines():
        if "[file join $::quartus(qip_path)" not in line:
            continue
        m = re.search(r'\[file join \$::quartus\(qip_path\)\s+"([^"]+)"\]', line)
        if m and not (root / m.group(1)).is_file():
            errors.append(f"QIP path does not resolve: {m.group(1)}")
    required = ("SDC_ENTITY_FILE", "TCL_ENTITY_FILE", "SYSTEMVERILOG_FILE", "VERILOG_FILE")
    for key in required:
        if key not in text:
            errors.append(f"QIP lost required RTL/timing assignment class: {key}")
    if "RZQ_GROUP" not in text or "IO_RELATIVE_LOCATION_ASSIGNMENT" not in text:
        errors.append("QIP lost electrical or relative placement assignments")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-clock-mhz", required=True, choices=tuple(SOURCES))
    args = parser.parse_args()
    source_build, provenance = SOURCES[args.memory_clock_mhz]
    source = REPO / source_build / PHY_REL
    destination = REPO / "rtl/primitive_backend" / args.memory_clock_mhz
    if not source.is_dir():
        raise SystemExit(f"Missing generated PHY source tree: {source}")
    if destination.exists():
        raise SystemExit(f"Refusing to overwrite existing extraction: {destination}")
    qips = list(source.rglob("*.qip"))
    if len(qips) != 1:
        raise SystemExit(f"Expected exactly one PHY QIP in {source}, found {len(qips)}")

    dest_parent = destination.parent
    dest_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{args.memory_clock_mhz}-stage-", dir=dest_parent))
    staged_tree = staging / "phy_probe_phy"
    try:
        shutil.copytree(source, staged_tree, symlinks=False)
        staged_qip = staged_tree / qips[0].relative_to(source)
        removed = prune_qip(staged_qip)
        errors = check_qip(staged_tree, staged_qip)
        if errors:
            raise SystemExit("Extraction checks failed:\n" + "\n".join(errors))

        source_hashes = inventory(source)
        extracted_hashes = inventory(staged_tree)
        primitives = primitive_inventory(staged_tree)
        # Only the QIP itself may differ; every copied RTL, include, and SDC
        # remains byte-identical to the generated source.
        qip_rel = staged_qip.relative_to(staged_tree).as_posix()
        changed = [p for p in source_hashes if source_hashes[p] != extracted_hashes.get(p)]
        if changed != [qip_rel]:
            raise SystemExit(f"Unexpected changed or missing copied files: {changed}")
        manifest = {
            "schema": 1,
            "backend": "frozen direct-RTL primitive backend; migration stage",
            "scope": "retains Altera PHY Lite fabric mapping/adaptation and calibration interface; not an independent or primitive-only PHY",
            "memory_clock_mhz": int(args.memory_clock_mhz),
            "source_tree": source_build + "/" + PHY_REL.as_posix(),
            "source_provenance": provenance,
            "source_phy_qip": qips[0].relative_to(source).as_posix(),
            "source_file_count": len(source_hashes),
            "source_sha256": source_hashes,
            "extracted_file_count": len(extracted_hashes),
            "extracted_sha256": extracted_hashes,
            "explicit_tennm_primitive_instance_count": sum(primitives.values()),
            "explicit_tennm_primitive_instances_by_module": primitives,
            "removed_qip_generation_metadata": removed,
            "preserved_qip_electrical_timing_and_rtl": True,
            "calibration_ip_copied": False,
        }
        (staged_tree / "EXTRACTION_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Atomic publication; destination was checked absent before starting.
        os.rename(staged_tree, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(f"Extracted {args.memory_clock_mhz} MHz frozen PHY snapshot to {destination.relative_to(REPO)}")
    print(f"Source files: {len(source_hashes)}; QIP metadata assignments removed: {len(removed)}")
    print("Copied RTL/SDC files retain their source hashes; no calibration IP or generation metadata is included.")


if __name__ == "__main__":
    main()
