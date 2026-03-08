FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# System deps, pip installs, and cleanup in a single layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    curl \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libffi-dev \
    libglib2.0-0 \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
