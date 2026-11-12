ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache tzdata \
    && pip install --no-cache-dir paho-mqtt==2.1.0 PyYAML==6.0.2 websocket-client==1.8.0

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
