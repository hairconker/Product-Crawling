#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "${PY}" ]]; then
  PY="${PYTHON:-python3}"
fi

WEEK="${WEEK:-2026-W17}"
PAGES="${PAGES:-1}"
PLATFORMS="${PLATFORMS:-xianyu}"
CATEGORIES="${CATEGORIES:-all}"
NO_ALT_TITLES="${NO_ALT_TITLES:-1}"
FAST="${FAST:-0}"

IFS=',' read -r -a PLATFORM_LIST <<< "${PLATFORMS}"
PLATFORM_ARGS=()
for platform in "${PLATFORM_LIST[@]}"; do
  platform="$(echo "${platform}" | xargs)"
  if [[ -n "${platform}" ]]; then
    PLATFORM_ARGS+=(--only "${platform}")
  fi
done

ARGS=(--from-dict "${WEEK}" --pages "${PAGES}" --resume)
if [[ "${NO_ALT_TITLES}" == "1" ]]; then
  ARGS+=(--no-alt-titles)
fi
if [[ "${FAST}" == "1" ]]; then
  ARGS+=(--fast)
fi
if [[ -n "${CATEGORIES}" && "${CATEGORIES}" != "all" ]]; then
  ARGS+=(--category "${CATEGORIES}")
fi

mkdir -p logs state data
echo "[dataset] start week=${WEEK} platforms=${PLATFORMS} pages=${PAGES} categories=${CATEGORIES}"
echo "[dataset] crawler args: ${ARGS[*]} ${PLATFORM_ARGS[*]}"

"${PY}" run_cpu_crawl_pw.py "${PLATFORM_ARGS[@]}" "${ARGS[@]}" "$@"
CRAWL_RC=$?

echo "[dataset] crawler exit=${CRAWL_RC}; exporting database files"
"${PY}" scripts/export_prices_db.py \
  --week "${WEEK}" \
  --sqlite "data/price_crawl_${WEEK}.sqlite" \
  --mysql-sql "data/price_crawl_${WEEK}_mysql.sql"
EXPORT_RC=$?
echo "[dataset] export exit=${EXPORT_RC}"

if [[ "${CRAWL_RC}" -ne 0 ]]; then
  exit "${CRAWL_RC}"
fi
exit "${EXPORT_RC}"
