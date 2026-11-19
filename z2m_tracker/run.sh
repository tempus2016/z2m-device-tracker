#!/usr/bin/with-contenv bashio
set -e

# ZHA mode (beta): the app reads Home Assistant's ZHA integration. It only uses the broker once, to
# remove the sensors it made over MQTT while it read Zigbee2MQTT, so the broker is still looked up.
ZHA="false"
if [ "$(bashio::config 'zigbee_stack' 'z2m')" = "zha" ]; then
    ZHA="true"
fi

if bashio::config.has_value 'mqtt_host'; then
    # A broker set in this app's options wins over the one Home Assistant's Supervisor offers.
    export MQTT_HOST="$(bashio::config 'mqtt_host')"
    export MQTT_PORT="$(bashio::config 'mqtt_port' '1883')"
    export MQTT_USER="$(bashio::config 'mqtt_user' '')"
    export MQTT_PASS="$(bashio::config 'mqtt_password' '')"
    export MQTT_SSL="$(bashio::config 'mqtt_ssl' 'false')"
    export MQTT_SSL_VERIFY="$(bashio::config 'mqtt_ssl_verify' 'true')"
    export MQTT_MANUAL="true"
    BROKER="MQTT ${MQTT_HOST}:${MQTT_PORT}, from this app's options"
# Asked directly: bashio::services.available logs "Service not enabled" as an error when there is no broker.
elif curl -fs -o /dev/null -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/services/mqtt; then
    export MQTT_HOST="$(bashio::services mqtt 'host')"
    export MQTT_PORT="$(bashio::services mqtt 'port')"
    export MQTT_USER="$(bashio::services mqtt 'username')"
    export MQTT_PASS="$(bashio::services mqtt 'password')"
    export MQTT_SSL="$(bashio::services mqtt 'ssl')"
    BROKER="MQTT ${MQTT_HOST}:${MQTT_PORT}"
else
    BROKER="no MQTT broker yet"
fi
if [ "${ZHA}" = "true" ]; then
    bashio::log.info "Starting Z2M Device Tracker (ZHA, beta)"
else
    bashio::log.info "Starting Z2M Device Tracker (${BROKER})"
fi
exec python3 -u /app/main.py
