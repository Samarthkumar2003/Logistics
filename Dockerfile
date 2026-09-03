# Logistics Copilot API.
#
# Build:  docker build -t logistics-copilot .
# Run:    docker run -p 8001:8001 --env-file .env logistics-copilot
#
# Secrets are injected at runtime, never baked in: .env and service_account.json
# are excluded by .dockerignore. See Documentation/04-running-locally.md.

FROM python:3.12-slim

# 3.12 matches .github/workflows/daily_report.yml. The floor is 3.10 — several
# modules annotate `int | None` in signatures that are evaluated at runtime, so
# 3.9 fails at import, not at type-check time.

# Bytecode caching is pointless in a layer that never changes; unbuffered output
# is what lets the platform collect logs as they happen rather than at exit.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before source, so editing a .py file does not re-resolve them.
#
# requirements.lock, not requirements.txt: every one of the 16 direct
# dependencies in requirements.txt is an unpinned `>=`, so two builds a week
# apart install different code. --no-deps is the point of a lock file — the lock
# already names every transitive package, so pip installs exactly what is listed
# and resolves nothing. A dependency missing from the lock fails the build here,
# loudly, instead of at import time in production.
#
# Regenerate after changing requirements.txt:
#   uv pip compile requirements.txt --python-version 3.12 -o requirements.lock
COPY requirements.lock .
RUN pip install --no-deps -r requirements.lock

# data/ is needed: agent_repo falls back to data/agents_database.csv when the
# agents table is empty. sql/ and Documentation/ are excluded by .dockerignore.
COPY backend/ ./backend/
COPY data/ ./data/

# Non-root. automation_state.json is written to PROJECT_ROOT at runtime, so /app
# has to be writable by the runtime user — hence chown rather than a bare USER.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

# Container logging defaults. Both are overridable with --env / --env-file.
#
# LOG_TO_FILE=0: a rotating file inside a container is written to the overlay
# filesystem and lost with the container. stdout is the log; the platform keeps it.
# LOG_JSON=1: correlation ids (request_id / job_id / scan_id / email_id) become
# queryable fields instead of text inside the message.
ENV LOG_TO_FILE=0 \
    LOG_JSON=1

# Deliberately 0, against config.py's default of True.
#
# The scheduler starts in the lifespan handler, so EVERY process running lifespan
# gets its own copy of every background job. Scale this image to 3 replicas
# with the default and you get three schedulers scanning the same inbox every 5
# minutes. The atomic claim in the scan keeps that correct — no email is
# processed twice — but it happens after classification, so you pay OpenAI three
# times for identical work, silently and indefinitely.
#
# Opting in explicitly (RUN_SCHEDULER=1 on exactly one replica) inverts the
# failure: forgetting it means no background work, which is loud. Lifespan logs
# "RUN_SCHEDULER=0 — no background jobs in this process" at boot, and nothing
# gets ingested, so it surfaces in minutes. Silent triple billing does not.
ENV RUN_SCHEDULER=0

# The port the app listens on. 8001 locally, because 8000 is taken on the dev
# machine; a platform that injects its own PORT overrides it at runtime.
ENV PORT=8001

EXPOSE 8001

# /health returns 200 when Supabase is reachable, 503 when it is not, so the
# status code alone is the signal. Python rather than curl: this image has no
# curl and adding one would mean an apt layer for a single request.
#
# Reads $PORT so it still probes the right socket when the platform reassigns it.
# Railway ignores this HEALTHCHECK entirely and polls healthcheckPath from
# railway.toml over its own network — this one is for `docker run` locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8001')+'/health', timeout=4).status == 200 else 1)"

# No --reload: it runs a file-watching supervisor that restarts the app on source
# changes, and the source in an image does not change. See run_backend.sh, which
# uses it for development.
#
# One worker. Scale with replicas, not --workers, because --workers runs the
# lifespan handler once per worker and each one starts its own scheduler — the
# duplicate-spend problem above, inside a single container where RUN_SCHEDULER
# cannot distinguish them.
#
# Shell form, not the exec-form list, because $PORT has to be expanded and the
# list form passes it to uvicorn as the literal string "$PORT". Railway assigns a
# port per service and injects it as PORT; a container that ignores it binds a
# socket the platform's proxy never connects to, and the deploy fails its
# healthcheck with an app that is running perfectly on the wrong number.
#
# `exec` matters as much as the expansion: without it, sh stays alive as PID 1 and
# uvicorn is its child, so SIGTERM on shutdown reaches the shell and not the app.
# The scheduler's jobs would then be killed mid-flight instead of at a checkpoint.
CMD ["sh", "-c", "exec uvicorn backend.app.api:app --host 0.0.0.0 --port ${PORT:-8001}"]
