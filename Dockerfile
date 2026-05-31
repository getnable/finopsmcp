# Build stage: compile C extensions, then discard build tooling
FROM python:3.12-slim AS builder
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libssl-dev libsasl2-dev git && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY templates/ ./templates/

RUN pip install --no-cache-dir ".[all]"

# Runtime stage: only the installed packages, no build tooling
FROM python:3.12-slim AS runtime
WORKDIR /app

# Runtime shared libs needed by some optional connectors
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 libsasl2-2 curl && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages and entry-point scripts from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/finops* /usr/local/bin/
COPY --from=builder /build/src /app/src
COPY --from=builder /build/templates /app/templates

RUN useradd -r -u 1000 -d /home/nable -s /bin/bash -m nable && \
    mkdir -p /data && chown nable /data

ENV FINOPS_DATA_DIR=/data

VOLUME ["/data"]
EXPOSE 8080

USER nable

ENTRYPOINT ["finops"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
