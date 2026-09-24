# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Trade-Bot runtime image.
#
# Pins Python 3.11 to match pyproject.toml (`requires-python = ">=3.11,<3.12"`).
# Multi-stage: builder installs deps into a venv; runtime copies just the venv
# and the source, so the final image has no pip/build toolchain.
# ---------------------------------------------------------------------------

FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps -e .

# ---------------------------------------------------------------------------

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 tradebot

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=tradebot:tradebot . /app

USER tradebot

EXPOSE 8000

# Bind to all interfaces inside the container; the lifespan validator's
# loopback guard is bypassed only via the operator-set API_HOST override
# handled by compose / orchestrator secrets — never bake it in here.
CMD ["python", "-m", "src.api"]
