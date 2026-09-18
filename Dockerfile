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
