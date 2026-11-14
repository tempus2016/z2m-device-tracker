#!/usr/bin/with-contenv bashio
set -e

# Asked directly: bashio::services.available logs "Service not enabled" as an error when there is no broker.
if curl -fs -o /dev/null -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/services/mqtt; then
    export MQTT_HOST="$(bashio::services mqtt 'host')"
    export MQTT_PORT="$(bashio::services mqtt 'port')"
    export MQTT_USER="$(bashio::services mqtt 'username')"
    export MQTT_PASS="$(bashio::services mqtt 'password')"
    export MQTT_SSL="$(bashio::services mqtt 'ssl')"
    bashio::log.info "Starting Z2M Device Tracker (MQTT ${MQTT_HOST}:${MQTT_PORT})"
else
    bashio::log.info "Starting Z2M Device Tracker (no MQTT broker yet)"
fi
exec python3 -u /app/main.py
