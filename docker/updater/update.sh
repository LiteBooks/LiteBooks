#!/usr/bin/env bash
# LiteBooks updater sidecar.
#
# Watches the shared state volume for an update request written by the web
# container, then performs the privileged work the web container cannot do:
# back up the database, pull the new image, restart the web service, and verify
# that the new build is actually serving.
#
# On failure it stops and reports. It deliberately does NOT roll back: the web
# entrypoint runs `migrate` on start, Django migrations are not reliably
# reversible, and putting the old image back over a migrated database would
# leave an old binary on a new schema. The pre-update dump is the recovery path.

set -uo pipefail

STATE_DIR="${LITEBOOKS_STATE_DIR:-/state}"
BACKUP_DIR="${STATE_DIR}/backups"
REQUEST_FILE="${STATE_DIR}/request.json"
STATUS_FILE="${STATE_DIR}/status.json"
SEEN_FILE="${STATE_DIR}/.last-run-id"

COMPOSE_FILE="${LITEBOOKS_COMPOSE_FILE:-/repo/compose.yaml}"
ENV_FILE="${LITEBOOKS_ENV_FILE:-/repo/.env}"
PROJECT="${LITEBOOKS_COMPOSE_PROJECT:-litebooks}"

IMAGE="${LITEBOOKS_IMAGE:-ghcr.io/litebooks/litebooks}"
HEALTH_URL="${LITEBOOKS_HEALTH_URL:-http://web:8000/healthz/}"
POLL_SECONDS="${LITEBOOKS_POLL_SECONDS:-5}"
HEALTH_TIMEOUT="${LITEBOOKS_HEALTH_TIMEOUT:-180}"
BACKUP_KEEP="${LITEBOOKS_BACKUP_KEEP:-5}"

RUN_ID=""
RUN_STATE=""
RUN_STEP=""
RUN_BACKUP=""
LOG_LINES=()

compose() {
  docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"
}

write_status() {
  local payload temp
  payload="$(jq -n \
    --argjson id "${RUN_ID:-0}" \
    --arg state "${RUN_STATE}" \
    --arg step "${RUN_STEP}" \
    --arg backup "${RUN_BACKUP}" \
    --arg log "$(printf '%s\n' "${LOG_LINES[@]+"${LOG_LINES[@]}"}")" \
    '{id: $id, state: $state, step: $step, backup_path: $backup, log: ($log | rtrimstr("\n") | split("\n"))}')"
  temp="$(mktemp "${STATE_DIR}/.status-XXXXXX")"
  printf '%s' "${payload}" > "${temp}"
  chmod 0644 "${temp}"
  mv "${temp}" "${STATUS_FILE}"
}

log() {
  local line="$*"
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${line}" >&2
  LOG_LINES+=("${line}")
  write_status
}

step() {
  RUN_STEP="$1"
  log "== $2"
}

# Run a command, folding its output into the status log, and return ITS exit
# status. Piping into `while read` would lose both: the status would belong to
# the `while`, and the log appends would happen in a subshell and be discarded.
run_logged() {
  local output_file status line
  output_file="$(mktemp)"
  "$@" > "${output_file}" 2>&1
  status=$?
  while IFS= read -r line; do
    [ -n "${line}" ] && LOG_LINES+=("  ${line}")
  done < "${output_file}"
  rm -f "${output_file}"
  write_status
  return "${status}"
}

fail() {
  RUN_STATE="failed"
  log "$1"
  if [ -n "${RUN_BACKUP}" ]; then
    LOG_LINES+=(
      ""
      "The database backup taken before this update is at:"
      "  ${RUN_BACKUP}"
      ""
      "Migrations may already have been applied, so the update was NOT reverted."
      "To restore the previous state manually, on the Docker host:"
      "  docker compose stop web"
      "  gunzip -c <backup> | docker compose exec -T db sh -c 'psql -U \"\$POSTGRES_USER\" \"\$POSTGRES_DB\"'"
      "  # then set LITEBOOKS_TAG back in .env and: docker compose up -d web"
    )
  fi
  write_status
}

# Rewrite LITEBOOKS_TAG in .env *in place*. The file is bind-mounted as a single
# file, so anything that replaces the inode (sed -i, mv) silently detaches the
# mount and the host never sees the change. Truncate and rewrite instead.
persist_tag() {
  local tag="$1" content
  if [ ! -f "${ENV_FILE}" ]; then
    return 1
  fi
  if grep -q '^LITEBOOKS_TAG=' "${ENV_FILE}"; then
    content="$(awk -v tag="${tag}" '/^LITEBOOKS_TAG=/ {print "LITEBOOKS_TAG=" tag; next} {print}' "${ENV_FILE}")"
  else
    content="$(cat "${ENV_FILE}"; printf 'LITEBOOKS_TAG=%s\n' "${tag}")"
  fi
  printf '%s\n' "${content}" > "${ENV_FILE}"
}

take_backup() {
  local stamp path
  stamp="$(date -u '+%Y%m%d-%H%M%S')"
  path="${BACKUP_DIR}/pre-update-${1:-unknown}-${stamp}.sql.gz"
  mkdir -p "${BACKUP_DIR}"

  if ! compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "${path}"; then
    rm -f "${path}"
    return 1
  fi
  # An empty or trivially small dump means pg_dump failed inside the pipe.
  if [ ! -s "${path}" ] || [ "$(stat -c %s "${path}")" -lt 1024 ]; then
    rm -f "${path}"
    return 1
  fi

  RUN_BACKUP="${path}"
  log "Backup written to ${path} ($(du -h "${path}" | cut -f1))"

  # Keep only the newest N backups.
  local stale
  stale="$(ls -1t "${BACKUP_DIR}"/pre-update-*.sql.gz 2>/dev/null | tail -n "+$((BACKUP_KEEP + 1))")"
  while IFS= read -r old; do
    [ -n "${old}" ] || continue
    rm -f "${old}" && log "Pruned old backup $(basename "${old}")"
  done <<< "${stale}"
  return 0
}

verify_health() {
  local expected_sha="$1" deadline response served
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))

  while [ "$(date +%s)" -lt "${deadline}" ]; do
    response="$(curl -fsS --max-time 5 "${HEALTH_URL}" 2>/dev/null)"
    if [ -n "${response}" ]; then
      served="$(printf '%s' "${response}" | jq -r '.sha // ""' 2>/dev/null)"
      if [ -n "${served}" ] && [ "${served:0:7}" = "${expected_sha:0:7}" ]; then
        log "Health check passed: serving ${served:0:7}"
        return 0
      fi
      log "Waiting for the new build (health reports ${served:0:7} so far)"
    fi
    sleep 3
  done
  return 1
}

run_update() {
  local target_sha target_tag from_sha previous_tag
  target_sha="$1"
  target_tag="$2"
  from_sha="$3"

  RUN_STATE="running"
  LOG_LINES=()
  RUN_BACKUP=""
  log "Updating LiteBooks from ${from_sha:0:7} to ${target_sha:0:7} (${target_tag})"

  previous_tag="$(grep '^LITEBOOKS_TAG=' "${ENV_FILE}" 2>/dev/null | head -1 | cut -d= -f2-)"
  log "Current image tag: ${previous_tag:-main}"

  step "backup" "Backing up the database"
  if ! take_backup "${from_sha:0:7}"; then
    fail "Database backup failed. The update was stopped before anything changed."
    return
  fi

  step "pull" "Pulling ${IMAGE}:${target_tag}"
  if ! LITEBOOKS_TAG="${target_tag}" run_logged docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" pull web; then
    fail "Could not pull ${IMAGE}:${target_tag}. Nothing was changed."
    return
  fi

  step "apply" "Starting the new version"
  if ! persist_tag "${target_tag}"; then
    fail "Could not write the new tag to .env. Nothing was changed."
    return
  fi
  if ! LITEBOOKS_TAG="${target_tag}" run_logged docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d web; then
    fail "Starting the new container failed."
    return
  fi

  step "verify" "Waiting for LiteBooks to come back"
  if ! verify_health "${target_sha}"; then
    log "--- recent web container logs ---"
    run_logged docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" logs --tail 60 web
    fail "The new version did not become healthy within ${HEALTH_TIMEOUT}s."
    return
  fi

  RUN_STATE="success"
  RUN_STEP="done"
  log "Update complete. Now running ${target_sha:0:7}."
  write_status
}

main() {
  mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"
  chmod 0777 "${STATE_DIR}" "${BACKUP_DIR}"

  echo "LiteBooks updater watching ${REQUEST_FILE} (project ${PROJECT})" >&2

  while true; do
    if [ -f "${REQUEST_FILE}" ]; then
      local local_id seen_id
      local_id="$(jq -r '.id // empty' "${REQUEST_FILE}" 2>/dev/null)"
      seen_id="$(cat "${SEEN_FILE}" 2>/dev/null || echo "")"

      if [ -n "${local_id}" ] && [ "${local_id}" != "${seen_id}" ]; then
        printf '%s' "${local_id}" > "${SEEN_FILE}"
        RUN_ID="${local_id}"
        run_update \
          "$(jq -r '.target_sha // empty' "${REQUEST_FILE}")" \
          "$(jq -r '.target_tag // empty' "${REQUEST_FILE}")" \
          "$(jq -r '.from_sha // empty' "${REQUEST_FILE}")"
      fi
    fi
    sleep "${POLL_SECONDS}"
  done
}

main "$@"
