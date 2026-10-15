#!/usr/bin/env bash
# Isolated public PHY generation; preserves previously generated builds.
set -euo pipefail
cd "$(dirname "$0")/.."
freq=${1:-800}
ref_freq=${2:-200}
tag=${3:-strobe-hybrid-800}
[[ "$freq" == 800 || "$freq" == 900 || "$freq" == 1000 || "$freq" == 1100 || "$freq" == 1200 || "$freq" == 1250 ]]
[[ "$ref_freq" =~ ^[0-9]+([.][0-9]+)?$ ]]
case "$tag" in
    strobe-hybrid-800|strobe-hybrid-800-ref100) [[ "$freq" == 800 ]] ;;
    strobe-hybrid-900-ref100) [[ "$freq" == 900 && "$ref_freq" == 100 ]] ;;
    strobe-hybrid-1000-ref100) [[ "$freq" == 1000 && "$ref_freq" == 100 ]] ;;
    strobe-hybrid-1100-ref100) [[ "$freq" == 1100 && "$ref_freq" == 100 ]] ;;
    strobe-hybrid-1200-ref100) [[ "$freq" == 1200 && "$ref_freq" == 100 ]] ;;
    strobe-hybrid-1250-ref100) [[ "$freq" == 1250 && "$ref_freq" == 100 ]] ;;
    strobe-hybrid-1250-ref50) [[ "$freq" == 1250 && "$ref_freq" == 50 ]] ;;
    strobe-hybrid-1250-ref125) [[ "$freq" == 1250 && "$ref_freq" == 125 ]] ;;
    *) exit 2 ;;
esac
dst="build/$tag"
mkdir -p "$dst" logs
[[ ! -e "$dst/phy_probe.qsys" ]] || { echo "Existing PHY: refuse to overwrite $dst"; exit 2; }
printf 'set mode hybrid\nset freq %s\nset ref_freq %s\n' "$freq" "$ref_freq" > "$dst/probe_config.tcl"
printf 'PROJECT_REVISION = phy_probe\n' > "$dst/phy_probe.qpf"
printf 'set_global_assignment -name FAMILY "Agilex 5"\nset_global_assignment -name DEVICE A5ED065BB32AE6SR0\n' > "$dst/phy_probe.qsf"
cd "$dst"
qsys-script --quartus-project=phy_probe.qpf --script=../../scripts/generate_public_phy.tcl > "../../logs/generate-$tag.log" 2>&1
qsys-generate phy_probe.qsys --quartus-project=phy_probe.qpf --synthesis=VERILOG > "../../logs/synthesis-$tag.log" 2>&1
