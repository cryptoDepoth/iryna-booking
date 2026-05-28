FROM python:3.12-slim

WORKDIR /app

# Runtime dependency for client emails and e-Transfer auto-confirmation.
# Himalaya releases use .tgz and contain a flat binary at archive root.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tar fonts-dejavu-core \
    && curl -sL -o /tmp/himalaya.tgz \
       "https://github.com/pimalaya/himalaya/releases/download/v1.2.0/himalaya.x86_64-linux.tgz" \
    && tar -xzf /tmp/himalaya.tgz -C /usr/local/bin/ --strip-components=0 \
    && chmod +x /usr/local/bin/himalaya \
    && himalaya --version \
    && rm -rf /var/lib/apt/lists/* /tmp/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["/bin/bash", "/app/start.sh"]
