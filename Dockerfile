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

# ---------------------------------------------------------------- cua ------
# The automation runtime. Headed Chromium on a virtual display, because the
# escalation path has to be able to hand this exact session to a person.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble AS cua

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# x11vnc twice over one Xvfb display gives the two endpoints the escalation
# model needs: a view-only socket to watch, and an interactive one to drive.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb x11-utils x11vnc novnc websockify \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    PYTHONPATH=/work/src \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /work

# The base image ships the browser builds under /ms-playwright but not the
# Python package. Pin it to the image tag so the driver and the browser
# revisions agree.
RUN uv pip install --system --no-cache \
      "playwright==1.55.0" \
      "pydantic>=2.7" "structlog>=24.1" "pyyaml>=6.0" \
      "openai>=1.40" "httpx>=0.27" "python-dotenv>=1.0" \
      "fastapi>=0.111" "uvicorn[standard]>=0.30" "python-multipart>=0.0.9" \
      "pytest>=8" "pytest-asyncio>=0.23" "jsonschema>=4.22"

COPY docker/cua-entrypoint.sh /usr/local/bin/cua-entrypoint
RUN chmod +x /usr/local/bin/cua-entrypoint

COPY . /work

# 8080 operator console, 8081 capability API, 6080 monitor VNC,
# 6081 interactive VNC.
EXPOSE 8080 8081 6080 6081

ENTRYPOINT ["cua-entrypoint"]
CMD ["sleep", "infinity"]

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

# FastAPI is here but Playwright deliberately is not: the point of this image
# is that everything above the surface seam runs with no browser driver at all.
RUN uv pip install --system --no-cache \
      "pydantic>=2.7" "structlog>=24.1" "pyyaml>=6.0" \
      "fastapi>=0.111" "httpx>=0.27" \
      "pytest>=8" "pytest-asyncio>=0.23" "jsonschema>=4.22"

COPY . /work

# `not integration` as well as `not llm`: this image has no browser, so the
# integration tests cannot pass here and their absence is the point. `make
# test-schema` passes the same selection explicitly; this is the default for
# anyone who runs the image with no command.
CMD ["pytest", "-q", "-m", "not llm and not integration"]
