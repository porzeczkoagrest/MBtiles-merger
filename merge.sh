#!/bin/bash
# Merge all input .mbtiles files from data/ into data/merged.mbtiles.
set -euo pipefail

ROOT_DIR="$(dirname "$(readlink -f "$0")")"
DATA_DIR="$ROOT_DIR/data"
MERGER="$ROOT_DIR/mbtiles_merge_fast.py"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Collect all .mbtiles files, excluding a previous merged output.
mapfile -t files < <(find "$DATA_DIR" -maxdepth 1 -name "*.mbtiles" ! -name "merged.mbtiles" | sort)

if [ "${#files[@]}" -eq 0 ]; then
    echo "No .mbtiles files found in data/."
    echo "Place input files in: $DATA_DIR"
    exit 1
fi

if [ ! -f "$MERGER" ]; then
    echo "Missing merger script: $MERGER"
    exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python executable not found: $PYTHON_BIN"
    exit 1
fi

echo "Merging ${#files[@]} file(s):"
for f in "${files[@]}"; do
    echo "  - $(basename "$f")"
done
echo ""

rm -f "$DATA_DIR/merged.mbtiles"

"$PYTHON_BIN" "$MERGER" "$DATA_DIR/merged.mbtiles" "${files[@]}"

echo ""
echo "Done. Output: $DATA_DIR/merged.mbtiles"
