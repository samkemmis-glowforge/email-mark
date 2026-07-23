# email-mark ("Mark") — Slack Socket Mode bot for Cloud Run / any Docker host.
# Pure Python worker (no browser/rendering). Binds $PORT for the platform's
# startup health check; talks to Slack over an outbound WebSocket.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Cloud Run injects PORT (default 8080); run_bot.py's health listener binds it.
EXPOSE 8080
CMD ["python", "scripts/run_bot.py"]
