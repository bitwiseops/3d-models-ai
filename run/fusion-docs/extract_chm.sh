#!/usr/bin/env bash
# Stage 1 — Extract the Fusion API CHM into chm_html/ and flatten all *.htm to the top.
# CHM is the single source of truth: no network access anywhere in this pipeline.
# Idempotent: if chm_html/ already holds > 10k htm files, extraction is skipped.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHM_PATH="${CHM_PATH:-$HOME/Desktop/FusionAPI.chm}"
OUT="$HERE/chm_html"
THRESHOLD=10000

count_htm() { find "$OUT" -type f \( -iname '*.htm' -o -iname '*.html' \) 2>/dev/null | wc -l | tr -d ' '; }

if [[ ! -f "$CHM_PATH" ]]; then
  echo "ERROR: CHM not found at: $CHM_PATH" >&2
  echo "Set CHM_PATH to the .chm location and re-run." >&2
  exit 1
fi

mkdir -p "$OUT"

if [[ "$(count_htm)" -gt "$THRESHOLD" ]]; then
  echo "chm_html/ already populated ($(count_htm) htm files) — skipping extraction."
else
  echo "Extracting $CHM_PATH -> $OUT"
  if command -v 7z >/dev/null 2>&1; then
    7z x -y -o"$OUT" "$CHM_PATH" >/dev/null
  elif command -v 7za >/dev/null 2>&1; then
    7za x -y -o"$OUT" "$CHM_PATH" >/dev/null
  elif command -v extract_chmLib >/dev/null 2>&1; then
    extract_chmLib "$CHM_PATH" "$OUT" >/dev/null
  else
    echo "ERROR: no CHM extractor found." >&2
    echo "Install one of:" >&2
    echo "  brew install p7zip      # provides 7z/7za" >&2
    echo "  brew install chmlib     # provides extract_chmLib" >&2
    exit 1
  fi

  # Flatten: move every nested *.htm/*.html to the top of chm_html/.
  # Name collisions across subdirs are rare in CHM dumps; on collision keep first, log dupes.
  echo "Flattening nested htm files..."
  while IFS= read -r -d '' f; do
    base="$(basename "$f")"
    dest="$OUT/$base"
    if [[ "$f" == "$dest" ]]; then continue; fi
    if [[ -e "$dest" ]]; then
      # Disambiguate with a short hash of the source dir to avoid clobbering.
      sub="$(dirname "$f" | sed "s#^$OUT/##; s#/#_#g")"
      dest="$OUT/${sub}__${base}"
    fi
    mv "$f" "$dest"
  done < <(find "$OUT" -mindepth 2 -type f \( -iname '*.htm' -o -iname '*.html' \) -print0)
fi

N="$(count_htm)"
echo "CHECK (Stage 1): chm_html htm count = $N (threshold > $THRESHOLD)"
if [[ "$N" -le "$THRESHOLD" ]]; then
  echo "FAIL: fewer than $THRESHOLD htm files — CHM structure may differ from expectation. STOP." >&2
  exit 2
fi
echo "PASS (Stage 1)"
