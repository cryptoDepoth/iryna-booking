FROM python:3.11-slim

WORKDIR /app

# ── Install Himalaya CLI (latest x86_64 Linux binary) ────────────────────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tar \
    && HIMALAYA_URL="https://github.com/pimalaya/himalaya/releases/latest/download/himalaya-x86_64-unknown-linux-gnu.tar.gz" \
    && curl -sL -o /tmp/himalaya.tar.gz "$HIMALAYA_URL" \
    && tar -xzf /tmp/himalaya.tar.gz -C /usr/local/bin/ \
    && chmod +x /usr/local/bin/himalaya \
    && himalaya --version \
    && apt-get purge -y curl tar \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# ── Install Python deps ──────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application ─────────────────────────────────────────────────────────
COPY . .

# ── Entrypoint ───────────────────────────────────────────────────────────────
CMD ["/bin/bash", "/app/start.sh"]
