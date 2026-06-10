#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "${PY}" ]]; then
  PY="${PYTHON:-python3}"
fi

KEYWORDS="${KEYWORDS:-cpu}"
PAGES="${PAGES:-1}"
PLATFORMS="${PLATFORMS:-xianyu,taobao}"

IFS=',' read -r -a PLATFORM_LIST <<< "${PLATFORMS}"
PLATFORM_ARGS=()
for platform in "${PLATFORM_LIST[@]}"; do
  platform="$(echo "${platform}" | xargs)"
  if [[ -n "${platform}" ]]; then
    PLATFORM_ARGS+=(--only "${platform}")
  fi
done

mkdir -p logs state
exec "${PY}" run_cpu_crawl_pw.py \
  "${PLATFORM_ARGS[@]}" \
  --keywords "${KEYWORDS}" \
  --pages "${PAGES}" \
  "$@"
