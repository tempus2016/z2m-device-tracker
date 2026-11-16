#!/usr/bin/with-contenv bashio
set -e

if bashio::config.has_value 'mqtt_host'; then
    # A broker set in this app's options wins over the one Home Assistant's Supervisor offers.
    export MQTT_HOST="$(bashio::config 'mqtt_host')"
    export MQTT_PORT="$(bashio::config 'mqtt_port' '1883')"
    export MQTT_USER="$(bashio::config 'mqtt_user' '')"
    export MQTT_PASS="$(bashio::config 'mqtt_password' '')"
    export MQTT_SSL="$(bashio::config 'mqtt_ssl' 'false')"
    export MQTT_SSL_VERIFY="$(bashio::config 'mqtt_ssl_verify' 'true')"
    export MQTT_MANUAL="true"
    bashio::log.info "Starting Z2M Device Tracker (MQTT ${MQTT_HOST}:${MQTT_PORT}, from this app's options)"
# Asked directly: bashio::services.available logs "Service not enabled" as an error when there is no broker.
elif curl -fs -o /dev/null -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/services/mqtt; then
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
