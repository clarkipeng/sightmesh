#!/usr/bin/env bash
set -euo pipefail

PORT="${SMOKE_PORT:-43210}"
LABEL="${LABEL:-io.agent-deck.smoke.disposable}"
DRY_RUN="${DRY_RUN:-1}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "recovery-smoke: dry_run=${DRY_RUN} port=${PORT} label=${LABEL}"
echo "recovery-smoke: refusing to touch Conductor workers or unmanaged labels"

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "recovery-smoke: would start and stop a disposable Python HTTP server"
  exit 0
fi

tmpdir="$(mktemp -d)"
pidfile="${tmpdir}/server.pid"
cleanup() {
  if [[ -f "${pidfile}" ]]; then
    local pid
    pid="$(cat "${pidfile}")"
    kill "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  fi
  rm -rf "${tmpdir}"
}
trap cleanup EXIT

"${PYTHON_BIN}" -m http.server "${PORT}" --bind 127.0.0.1 --directory "${tmpdir}" >/dev/null 2>&1 &
echo "$!" >"${pidfile}"
sleep 0.5
kill -0 "$(cat "${pidfile}")" 2>/dev/null || {
  echo "recovery-smoke: disposable server failed to start on ${PORT}" >&2
  exit 1
}
curl --fail --silent "http://127.0.0.1:${PORT}/" >/dev/null
first_pid="$(cat "${pidfile}")"
kill "${first_pid}"
wait "${first_pid}" 2>/dev/null || true
rm "${pidfile}"
"${PYTHON_BIN}" -m http.server "${PORT}" --bind 127.0.0.1 --directory "${tmpdir}" >/dev/null 2>&1 &
echo "$!" >"${pidfile}"
sleep 0.5
kill -0 "$(cat "${pidfile}")" 2>/dev/null || {
  echo "recovery-smoke: disposable server failed to restart on ${PORT}" >&2
  exit 1
}
curl --fail --silent "http://127.0.0.1:${PORT}/" >/dev/null
echo "recovery-smoke: disposable restart verified"
