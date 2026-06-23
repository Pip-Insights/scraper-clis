#!/usr/bin/env bash
# Install EVERY CLI in this repo onto PATH. Generic and convention-driven: it
# discovers CLIs by layout, so adding a new one never means editing this script
# (or anything in a consumer like the PipInsights portal container).
#
# Convention (see README.md "Prescribed CLI format"): each CLI is a directory
# `<connector>/` containing an executable entrypoint `<connector>/<connector>.py`
# and, optionally, a `<connector>/requirements.txt`. `common/` is shared library
# code, not a CLI, and is skipped.
#
# For each CLI: install its requirements (if any), then symlink the entrypoint to
# /usr/local/bin/<connector> so `<connector> ...` runs it. The repo is left in
# place; the entrypoint resolves its own location via realpath, so the shared
# `common/` import keeps working through the symlink.
#
# Usage: ./install.sh [BIN_DIR]   (BIN_DIR defaults to /usr/local/bin)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${1:-/usr/local/bin}"
mkdir -p "$BIN"

installed=()
for dir in "$ROOT"/*/; do
  name="$(basename "$dir")"
  [ "$name" = "common" ] && continue
  entry="${dir}${name}.py"
  if [ ! -f "$entry" ]; then
    continue  # not a CLI by our convention; skip silently
  fi
  if [ -f "${dir}requirements.txt" ]; then
    echo "[$name] installing requirements"
    pip3 install --no-cache-dir -r "${dir}requirements.txt"
  fi
  chmod +x "$entry"
  ln -sf "$entry" "$BIN/$name"
  installed+=("$name")
done

if [ "${#installed[@]}" -eq 0 ]; then
  echo "install.sh: no CLIs found under $ROOT" >&2
  exit 1
fi
echo "installed ${#installed[@]} CLI(s) into $BIN: ${installed[*]}"
