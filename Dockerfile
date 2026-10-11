FROM python:3.12-slim

WORKDIR /app

# Build deps for camoufox's indexed-zstd C extension (gcc, g++, python
# headers, libzstd), Xvfb (camoufox's headless="virtual" mode shells out to
# it, and patchright's headful fallback also runs under it), and Firefox's
# runtime shared libs -- camoufox is a patched Firefox, so it needs the same
# system packages as the vanilla browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ python3-dev libzstd-dev xvfb curl \
        libgtk-3-0 libasound2 libdbus-glib-1-2 libxt6 libxtst6 \
        libgl1 libegl1 libxcomposite1 libxdamage1 libxrandr2 libpango-1.0-0 \
        fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Tailscale CLI only (no tailscaled here) -- this talks to the tailscale
# sidecar's daemon over a shared socket volume, so this container never
# needs NET_ADMIN or its own tailscaled. Same pattern as pornolab-seeder.
ARG TARGETARCH
RUN TS_ARCH=$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo amd64) \
    && curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_1.102.5_${TS_ARCH}.tgz" \
        -o /tmp/ts.tgz \
    && tar -xzf /tmp/ts.tgz -C /tmp \
    && mv "/tmp/tailscale_1.102.5_${TS_ARCH}/tailscale" /usr/local/bin/tailscale \
    && rm -rf /tmp/ts.tgz "/tmp/tailscale_1.102.5_${TS_ARCH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && camoufox fetch \
    && python -m patchright install --with-deps chromium

COPY app ./app

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
