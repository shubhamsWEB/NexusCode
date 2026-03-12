# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

# System deps (rarely changes — own layer for cache efficiency)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libffi-dev \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Python deps (cached by BuildKit — only re-runs when requirements.txt changes)
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

# Remove build-only compilers to shrink image
RUN apt-get purge -y --auto-remove gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Application source (changes most often — last layer)
COPY . ./

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
