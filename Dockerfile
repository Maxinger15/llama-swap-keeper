FROM python:3.13-alpine

ARG VERSION=dev
LABEL org.opencontainers.image.title="llama-swap-keeper" \
      org.opencontainers.image.description="Keeps a preferred llama-swap model warm while the server is idle" \
      org.opencontainers.image.source="https://github.com/Maxinger15/llama-swap-keeper" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="$VERSION"

RUN addgroup -S keeper && adduser -S -G keeper keeper
WORKDIR /app
COPY --chown=keeper:keeper keeper.py /app/keeper.py
USER keeper

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python3", "/app/keeper.py"]
