#!/usr/bin/env bash
set -euo pipefail

scope="${1:-}"
requirements="$(mktemp "${TMPDIR:-/tmp}/memtomem-stm-audit.XXXXXX")"
trap 'rm -f "$requirements"' EXIT

case "$scope" in
  runtime)
    export_args=(--no-dev)
    ;;
  extras)
    export_args=(--no-dev --all-extras)
    ;;
  dev)
    export_args=(--all-groups --all-extras)
    ;;
  *)
    echo "usage: $0 {runtime|extras|dev}" >&2
    exit 2
    ;;
esac

uv export --quiet --locked --no-emit-project "${export_args[@]}" -o "$requirements"
uv run --frozen --group audit pip-audit \
  --require-hashes --disable-pip --strict --requirement "$requirements"
