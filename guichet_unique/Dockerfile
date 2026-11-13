# syntax=docker/dockerfile:1.7
# Guichet Unique — image de l'add-on Home Assistant (construite par le Supervisor).

# ─── Stage 1 : builder ──────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:0.12.10-python3.12-trixie AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN uv venv /install
WORKDIR /app

# README.md est OBLIGATOIRE : hatchling le lit depuis pyproject.toml (champ `readme`).
COPY pyproject.toml uv.lock README.md ./
RUN UV_PROJECT_ENVIRONMENT=/install uv sync --frozen --no-dev --no-install-project \
      --extra scheduler --extra web
COPY src ./src
RUN uv pip install --python /install --no-deps --quiet .

# ─── Stage 2 : runtime ──────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.12/site-packages" \
    TZ=Europe/Paris \
    GUICHET_OUTPUT_DIR=/data

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /install
COPY run.sh /run.sh
RUN chmod +x /run.sh && guichet --help >/dev/null

# Tourne en root : /data (persistant, options.json) appartient à root dans les add-ons.
CMD ["/run.sh"]
