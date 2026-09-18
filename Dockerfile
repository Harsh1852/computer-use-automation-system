# Two images from one file.
#
#   target: app  -> the MERIDIAN CoreBank target application (Flask)
#   target: cua  -> the automation runtime (added in the surface phase:
#                   Playwright + Xvfb + the two VNC bridges)
#
# The target app gets a plain slim image on purpose: it must not share a
# filesystem or a Python environment with the thing automating it.

# ---------------------------------------------------------------- app ------
FROM python:3.11-slim AS app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /srv

RUN uv pip install --system --no-cache "flask>=3.0"

COPY apps/ /srv/apps/

EXPOSE 5000
CMD ["python", "-m", "apps.corebank.app"]

# --------------------------------------------------------------- test ------
# Runs the suite with no host-side Python install. Kept separate from `cua`
# so unit tests do not need a browser or an X server to run.
FROM python:3.11-slim AS test

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    PYTHONPATH=/work/src

WORKDIR /work

RUN uv pip install --system --no-cache \
      "pydantic>=2.7" "structlog>=24.1" "pyyaml>=6.0" \
      "pytest>=8" "pytest-asyncio>=0.23" "jsonschema>=4.22"

COPY . /work

CMD ["pytest", "-q", "-m", "not llm"]
