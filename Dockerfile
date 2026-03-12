# syntax=docker/dockerfile:1

# ── Stage 1: build ────────────────────────────────────────────────────────────
# Has compilers and build headers; nothing from here leaks into the final image.
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install into a venv so we can COPY just the venv to the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./

# pip cache mount speeds up rebuilds without baking the cache into the image.
RUN --mount=type=cache,id=pip-cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

# ── Shrink the venv before copying it ─────────────────────────────────────────
# torch/test     — test suite not needed at runtime           (~87 MB)
# torch/include  — C++ headers only used when compiling       (~62 MB)
# __pycache__ + *.pyc across all packages                     (~80 MB)
# NOTE: torch/distributed must be kept — torch.utils.data.dataloader imports it.
RUN rm -rf \
        /opt/venv/lib/python3.11/site-packages/torch/test \
        /opt/venv/lib/python3.11/site-packages/torch/include \
    && find /opt/venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
       find /opt/venv -name "*.pyc" -o -name "*.pyo" | xargs rm -f 2>/dev/null; true


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
# No compilers, no build headers — only what the app needs to run.
FROM python:3.11-slim

WORKDIR /app

# Runtime system libraries only (no *-dev packages).
# libpq5      — psycopg2 / asyncpg PostgreSQL client
# libpango*   — weasyprint PDF rendering
# libharfbuzz — weasyprint font shaping
# libcairo2   — weasyprint Cairo backend
# libglib2    — GLib (transitive dep of pango/cairo)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy the trimmed venv from the build stage.
COPY --from=builder /opt/venv /opt/venv

# Copy application source last (changes most often).
COPY . ./

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
