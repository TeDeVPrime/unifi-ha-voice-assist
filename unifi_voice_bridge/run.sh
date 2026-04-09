#!/usr/bin/with-contenv bashio
set -Eeuo pipefail

OPTIONS_FILE="/data/options.json"

bashio::log.info "Starting UniFi Voice Bridge..."

if [[ ! -f "${OPTIONS_FILE}" ]]; then
  bashio::log.error "Missing ${OPTIONS_FILE}"
  exit 1
fi

CONFIG_FILE="$(bashio::config 'config_file')"
LOGS_DIR="$(bashio::config 'logs_dir')"
CLIPS_DIR="$(bashio::config 'clips_dir')"

if [[ -z "${CONFIG_FILE}" ]]; then
  bashio::log.error "config_file is empty in add-on options."
  exit 1
fi

mkdir -p "${LOGS_DIR}"
mkdir -p "${CLIPS_DIR}"
mkdir -p "$(dirname "${CONFIG_FILE}")"

if [[ ! -f "${CONFIG_FILE}" ]]; then
  bashio::log.warning "Config file not found at ${CONFIG_FILE}"
  bashio::log.warning "Creating starter camera profile file..."

  cat > "${CONFIG_FILE}" <<'EOC'
global:
  language: "en"
  agent_id: "home_assistant"
  wake_word: "hey_unifi"
  require_known_face: true
  require_person_presence: true
  response_enabled_default: true

cameras: []
EOC
fi

bashio::log.info "Using config file: ${CONFIG_FILE}"
bashio::log.info "Logs directory: ${LOGS_DIR}"
bashio::log.info "Audio clips directory: ${CLIPS_DIR}"

exec python3 /app/main.py
