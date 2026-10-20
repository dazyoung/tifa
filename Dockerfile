# One image for both services; TIFA_ROLE (api | poller) picks the app at start-up.
# Base images are pinned by digest (both stages use the same Python image); Dependabot proposes updates.

FROM ghcr.io/astral-sh/uv:0.13.3@sha256:bee0a2a458fd956c5df7b8b4c5fed2257b0af34866e3da7b51db0be3acb375d1 AS uv

# ---- build: resolve the locked dependencies into /app/.venv, then install Tifa itself (not editable)
FROM python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime: the slim base, tzdata, the virtualenv and a non-root user; no build tools, no uv
FROM python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS runtime
# tzdata: London days and hours depend on the system time zone rules (kept current with each rebuild).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /var/log/apt /var/log/dpkg.log \
 && python -c "import zoneinfo; zoneinfo.ZoneInfo('Europe/London')" \
 && groupadd --system --gid 10001 tifa \
 && useradd --system --uid 10001 --gid tifa --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin tifa
COPY --from=build --chown=root:root /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PORT=8080
WORKDIR /app
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "tifa.main"]
