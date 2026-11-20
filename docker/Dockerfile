# Z2M Device Tracker without Supervisor, for Home Assistant Container (plain Docker).
# Build from the repository root: docker build -f docker/Dockerfile .
# The Python packages match z2m_tracker/Dockerfile (tests/test_versions.py checks they agree).
FROM python:3.13-alpine3.23

RUN apk add --no-cache tzdata \
    && pip install --no-cache-dir paho-mqtt==2.2.0 PyYAML==6.0.3 websocket-client==1.9.2

COPY z2m_tracker/app /app

ENV PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/login', timeout=4)"

CMD ["python3", "-u", "/app/main.py"]
