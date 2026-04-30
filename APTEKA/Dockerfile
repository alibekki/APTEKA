# APTEKA — pharmacy bot (Telegram + WhatsApp via Wazzup) + Flask admin panel.
# Single process: main.py boots the Telegram polling loop, spawns Flask in
# a daemon thread for the admin panel + Wazzup webhook on $FLASK_PORT (5001).

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimal runtime deps. SQLite ships with python; numpy needs libstdc++ which
# python:slim already has. Curl is used by Fly's healthcheck script and tini
# gives us proper PID-1 signal handling so SIGTERM gracefully stops the bot.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache-friendly: deps first, code second
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data lives on a Fly volume mounted at /data
ENV DATA_DIR=/data \
    FLASK_PORT=5001
RUN mkdir -p /data && chown -R 1000:1000 /data /app

# Run as non-root for safety
USER 1000:1000

EXPOSE 5001

# tini reaps zombies and forwards SIGTERM correctly to python
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "main.py"]
