#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="LiteBooks"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_EXAMPLE="${PROJECT_ROOT}/.env.example"
ENV_FILE="${PROJECT_ROOT}/.env"

MODE="auto"
START_APP="true"
FORCE_ENV="false"
BOOTSTRAP_OWNER="true"
ADMIN_USERNAME="admin"
ADMIN_PASSWORD=""
ADMIN_FIRST_NAME=""
ADMIN_LAST_NAME=""

log() {
  printf '%s\n' "==> $*"
}

warn() {
  printf '%s\n' "Warning: $*" >&2
}

die() {
  printf '%s\n' "Error: $*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
LiteBooks installer

Usage:
  ./install.sh [options]

Options:
  --mode auto|docker|local       Choose install path. Default: auto.
  --no-start                     Prepare dependencies and config, but do not start the app.
  --force-env                    Regenerate .env from .env.example.
  --skip-bootstrap               Do not create the first owner login.
  --admin-username USERNAME      First owner username. Default: admin.
  --admin-password PASSWORD      First owner password. Prompts if omitted.
  --admin-first-name NAME        First owner first name.
  --admin-last-name NAME         First owner last name.
  -h, --help                     Show this help text.

Examples:
  ./install.sh
  ./install.sh --admin-username owner
  ./install.sh --mode docker --skip-bootstrap
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      [[ $# -ge 2 ]] || die "--mode requires one of: auto, docker, local"
      MODE="$2"
      shift 2
      ;;
    --mode=*)
      MODE="${1#*=}"
      shift
      ;;
    --no-start)
      START_APP="false"
      shift
      ;;
    --force-env)
      FORCE_ENV="true"
      shift
      ;;
    --skip-bootstrap)
      BOOTSTRAP_OWNER="false"
      shift
      ;;
    --admin-username)
      [[ $# -ge 2 ]] || die "--admin-username requires a value"
      ADMIN_USERNAME="$2"
      shift 2
      ;;
    --admin-username=*)
      ADMIN_USERNAME="${1#*=}"
      shift
      ;;
    --admin-password)
      [[ $# -ge 2 ]] || die "--admin-password requires a value"
      ADMIN_PASSWORD="$2"
      shift 2
      ;;
    --admin-password=*)
      ADMIN_PASSWORD="${1#*=}"
      shift
      ;;
    --admin-first-name)
      [[ $# -ge 2 ]] || die "--admin-first-name requires a value"
      ADMIN_FIRST_NAME="$2"
      shift 2
      ;;
    --admin-first-name=*)
      ADMIN_FIRST_NAME="${1#*=}"
      shift
      ;;
    --admin-last-name)
      [[ $# -ge 2 ]] || die "--admin-last-name requires a value"
      ADMIN_LAST_NAME="$2"
      shift 2
      ;;
    --admin-last-name=*)
      ADMIN_LAST_NAME="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

case "${MODE}" in
  auto|docker|local) ;;
  *) die "--mode must be one of: auto, docker, local" ;;
esac

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

random_hex() {
  local bytes="${1:-32}"

  if command_exists openssl; then
    openssl rand -hex "${bytes}"
    return
  fi

  if command_exists python3; then
    python3 -c "import secrets; print(secrets.token_hex(${bytes}))"
    return
  fi

  die "openssl or python3 is required to generate secure install secrets."
}

create_env_file() {
  [[ -f "${ENV_EXAMPLE}" ]] || die "Missing .env.example. Cannot create ${ENV_FILE}."

  if [[ -f "${ENV_FILE}" && "${FORCE_ENV}" != "true" ]]; then
    log "Using existing .env"
    return
  fi

  local postgres_password secret_key temp_file
  postgres_password="$(random_hex 18)"
  secret_key="$(random_hex 48)"
  temp_file="${ENV_FILE}.tmp.$$"

  log "Creating .env with generated secrets"

  while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      DJANGO_SECRET_KEY=*) printf 'DJANGO_SECRET_KEY=%s\n' "${secret_key}" ;;
      POSTGRES_PASSWORD=*) printf 'POSTGRES_PASSWORD=%s\n' "${postgres_password}" ;;
      *) printf '%s\n' "${line}" ;;
    esac
  done < "${ENV_EXAMPLE}" > "${temp_file}"

  mv "${temp_file}" "${ENV_FILE}"
}

find_compose_file() {
  local candidate
  for candidate in compose.yaml compose.yml docker-compose.yml docker-compose.yaml; do
    if [[ -f "${PROJECT_ROOT}/${candidate}" ]]; then
      printf '%s\n' "${PROJECT_ROOT}/${candidate}"
      return 0
    fi
  done

  return 1
}

find_manage_py() {
  find "${PROJECT_ROOT}" -maxdepth 3 -name manage.py -type f -print -quit 2>/dev/null
}

has_python_dependency_file() {
  [[ -f "${PROJECT_ROOT}/requirements.txt" || -f "${PROJECT_ROOT}/pyproject.toml" ]]
}

choose_mode() {
  if [[ "${MODE}" != "auto" ]]; then
    printf '%s\n' "${MODE}"
    return
  fi

  if find_compose_file >/dev/null; then
    printf '%s\n' "docker"
    return
  fi

  if [[ -n "$(find_manage_py)" ]]; then
    printf '%s\n' "local"
    return
  fi

  die "${APP_NAME} source files are not present yet. Expected a Compose file or a Django manage.py."
}

preflight_install() {
  local selected_mode="$1"

  [[ -n "$(find_manage_py)" ]] || die "Missing Django manage.py. Add the ${APP_NAME} application source before running the installer."
  has_python_dependency_file || die "Missing requirements.txt or pyproject.toml. Add the Python dependency file before running the installer."

  if [[ "${selected_mode}" == "docker" ]]; then
    find_compose_file >/dev/null || die "Missing Compose file. Cannot start the ${APP_NAME} stack."
    [[ -f "${PROJECT_ROOT}/Dockerfile" ]] || die "Missing Dockerfile. Cannot build the ${APP_NAME} web image."
  fi
}

export_env_file() {
  local line

  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue

    if [[ "${line}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      export "${line}"
    fi
  done < "${ENV_FILE}"
}

prompt_admin_password() {
  if [[ "${BOOTSTRAP_OWNER}" != "true" || "${START_APP}" != "true" || -n "${ADMIN_PASSWORD}" ]]; then
    return
  fi

  if [[ ! -t 0 ]]; then
    warn "No terminal is available to prompt for the first owner password. Skipping bootstrap."
    BOOTSTRAP_OWNER="false"
    return
  fi

  local password confirm
  while true; do
    read -r -s -p "First owner password for ${ADMIN_USERNAME}: " password
    printf '\n'
    read -r -s -p "Confirm password: " confirm
    printf '\n'

    if [[ "${password}" != "${confirm}" ]]; then
      warn "Passwords did not match. Try again."
      continue
    fi

    if [[ "${#password}" -lt 8 ]]; then
      warn "Password must be at least 8 characters."
      continue
    fi

    ADMIN_PASSWORD="${password}"
    break
  done
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
  elif command_exists docker-compose; then
    COMPOSE_CMD=(docker-compose)
  else
    die "Docker Compose is required. Install the Docker Compose plugin or docker-compose."
  fi
}

wait_for_migrations() {
  local compose_file="$1"
  local timeout="${LITEBOOKS_MIGRATE_WAIT_TIMEOUT:-180}"
  local deadline=$((SECONDS + timeout))

  log "Waiting for database migrations to finish"
  while true; do
    if "${COMPOSE_CMD[@]}" -f "${compose_file}" --env-file "${ENV_FILE}" exec -T web \
        python manage.py migrate --check >/dev/null 2>&1; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      die "Timed out after ${timeout}s waiting for migrations. Check logs with: ${COMPOSE_CMD[*]} -f ${compose_file} logs web"
    fi
    sleep 3
  done
}

run_docker_install() {
  local compose_file
  compose_file="$(find_compose_file)" || die "No Compose file found. Add compose.yml or docker-compose.yml first."

  command_exists docker || die "Docker is required for Docker mode. Install Docker Desktop or Docker Engine, then rerun ./install.sh."
  compose_cmd
  docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker, then rerun ./install.sh."

  log "Installing ${APP_NAME} with Docker Compose"

  if [[ "${START_APP}" != "true" ]]; then
    "${COMPOSE_CMD[@]}" -f "${compose_file}" --env-file "${ENV_FILE}" build
    log "Skipping app startup because --no-start was passed"
    return
  fi

  "${COMPOSE_CMD[@]}" -f "${compose_file}" --env-file "${ENV_FILE}" up -d --build
  log "LiteBooks is starting at http://127.0.0.1:${LITEBOOKS_PORT:-8000}"

  if [[ "${BOOTSTRAP_OWNER}" == "true" ]]; then
    wait_for_migrations "${compose_file}"
    log "Creating first owner login if needed"
    "${COMPOSE_CMD[@]}" -f "${compose_file}" --env-file "${ENV_FILE}" exec -T web python manage.py bootstrap_litebooks \
      --username "${ADMIN_USERNAME}" \
      --password "${ADMIN_PASSWORD}" \
      --first-name "${ADMIN_FIRST_NAME}" \
      --last-name "${ADMIN_LAST_NAME}"
  fi
}

run_local_install() {
  local manage_py

  command_exists python3 || die "python3 is required for local mode."
  manage_py="$(find_manage_py)"

  log "Installing ${APP_NAME} locally"
  export_env_file

  if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    python3 -m venv "${PROJECT_ROOT}/.venv"
  fi

  "${PROJECT_ROOT}/.venv/bin/python" -m pip install --upgrade pip

  if [[ -f "${PROJECT_ROOT}/requirements.txt" ]]; then
    "${PROJECT_ROOT}/.venv/bin/python" -m pip install -r "${PROJECT_ROOT}/requirements.txt"
  else
    "${PROJECT_ROOT}/.venv/bin/python" -m pip install -e "${PROJECT_ROOT}"
  fi

  log "Running database migrations"
  "${PROJECT_ROOT}/.venv/bin/python" "${manage_py}" migrate

  if [[ "${BOOTSTRAP_OWNER}" == "true" && "${START_APP}" == "true" ]]; then
    log "Creating first owner login if needed"
    "${PROJECT_ROOT}/.venv/bin/python" "${manage_py}" bootstrap_litebooks \
      --username "${ADMIN_USERNAME}" \
      --password "${ADMIN_PASSWORD}" \
      --first-name "${ADMIN_FIRST_NAME}" \
      --last-name "${ADMIN_LAST_NAME}"
  fi

  log "Building frontend assets"
  if command_exists npm; then
    npm ci
    npm run build
  else
    warn "npm is not available. Frontend assets were not rebuilt."
  fi

  log "Collecting static files"
  "${PROJECT_ROOT}/.venv/bin/python" "${manage_py}" collectstatic --noinput || warn "Static collection skipped or not configured."

  if [[ "${START_APP}" == "true" ]]; then
    log "Starting development server at http://127.0.0.1:8000"
    exec "${PROJECT_ROOT}/.venv/bin/python" "${manage_py}" runserver 127.0.0.1:8000
  fi
}

main() {
  cd "${PROJECT_ROOT}"

  local selected_mode
  selected_mode="$(choose_mode)"
  preflight_install "${selected_mode}"
  create_env_file
  export_env_file
  prompt_admin_password

  case "${selected_mode}" in
    docker) run_docker_install ;;
    local) run_local_install ;;
    *) die "Unsupported install mode: ${selected_mode}" ;;
  esac

  log "${APP_NAME} install finished"
}

main
