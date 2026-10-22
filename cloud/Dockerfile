# CPU-only Bisq Agent Network sidecar. Pin image digest for production.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BAN_SOURCE=fixture \
    BAN_DB=/data/ban.sqlite3 \
    BAN_FIXTURES_DIR=/app/fixtures \
    BAN_BIND_HOST=127.0.0.1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY fixtures ./fixtures

RUN python -m pip install --no-cache-dir --no-deps . \
    && useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin ban \
    && mkdir /data && chown 10001:10001 /data

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8088

ENTRYPOINT ["ban"]
CMD ["serve", "--port", "8088"]
