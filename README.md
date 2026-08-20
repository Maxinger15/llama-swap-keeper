# llama-swap-keeper

[![CI](https://github.com/Maxinger15/llama-swap-keeper/actions/workflows/ci.yml/badge.svg)](https://github.com/Maxinger15/llama-swap-keeper/actions/workflows/ci.yml)
[![Release container](https://github.com/Maxinger15/llama-swap-keeper/actions/workflows/release.yml/badge.svg)](https://github.com/Maxinger15/llama-swap-keeper/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A tiny, stateless companion service that keeps your preferred
[llama-swap](https://github.com/mostlygeek/llama-swap) model warm.

When another model has not handled a request for the configured idle period,
llama-swap-keeper asks llama-swap to load the preferred model. It watches
in-flight requests so a long-running generation is not interrupted, emits
useful state-change logs, and otherwise stays quiet.

## Why?

llama-swap is excellent at loading the model a request needs. On a workstation
or home server, however, it is often useful to have one default model ready
between jobs. llama-swap-keeper adds that policy without changing llama-swap's
configuration or storing any state.

## Features

- Loads a preferred model after a configurable idle period
- Does nothing while the preferred model is already running
- Starts a fresh idle window when any non-preferred model is loaded manually
- Tracks llama-swap's SSE in-flight events to protect active requests
- Supports HTTP, HTTPS, optional Bearer API keys, and custom TLS verification
- Configuration exclusively through environment variables
- No database, volume, config file, web UI, or persistent state
- Low-noise logs: state changes and errors at `INFO`, poll details at `DEBUG`
- Multi-architecture release images for `linux/amd64` and `linux/arm64`

## Quick start

### Docker run

The keeper must be able to reach llama-swap. If both containers share a Docker
network named `llama-swap`, start it like this:

```bash
docker run -d \
  --name llama-swap-keeper \
  --restart unless-stopped \
  --network llama-swap \
  -e LLAMA_SWAP_URL=http://llama-swap:8080 \
  -e LLAMA_SWAP_MODEL=gemma-4-26B-A4B-it-qat-UD-Q4_K_XL \
  -e IDLE_TIMEOUT=4m \
  ghcr.io/maxinger15/llama-swap-keeper:latest
```

If llama-swap runs on the host, set `LLAMA_SWAP_URL` to an address reachable
from the container. Do not use `localhost` unless both processes share the same
network namespace.

### Docker Compose

```yaml
services:
  llama-swap-keeper:
    image: ghcr.io/maxinger15/llama-swap-keeper:latest
    container_name: llama-swap-keeper
    restart: unless-stopped
    environment:
      LLAMA_SWAP_URL: http://llama-swap:8080
      LLAMA_SWAP_MODEL: gemma-4-26B-A4B-it-qat-UD-Q4_K_XL
      IDLE_TIMEOUT: 4m
      POLL_INTERVAL: 15s
      TLS_VERIFY: "true"
      # LLAMA_SWAP_API_KEY: change-me
    networks:
      - llama-swap

networks:
  llama-swap:
    external: true
```

Then run:

```bash
docker compose up -d
docker compose logs -f llama-swap-keeper
```

The repository also includes a ready-to-edit [`compose.yaml`](compose.yaml).

## Configuration

Durations accept a number with an optional unit: `ms`, `s`, `m`, or `h`.
A bare number is interpreted as seconds.

| Environment variable | Required | Default | Description |
|---|:---:|---|---|
| `LLAMA_SWAP_MODEL` | Yes | — | Exact llama-swap model ID to keep warm. |
| `LLAMA_SWAP_URL` | No | `http://localhost:8080` | Base URL of the llama-swap API, without a trailing slash. |
| `IDLE_TIMEOUT` | No | `4m` | Time since the latest completed request to a different model or since a different model was detected as loaded. If neither exists, the timer starts when the keeper starts. |
| `POLL_INTERVAL` | No | `15s` | Interval between running-model and activity checks. |
| `REQUEST_TIMEOUT` | No | `30s` | Timeout for normal llama-swap API requests. |
| `LOAD_TIMEOUT` | No | `15m` | Timeout for the model-load request. Model startup can take much longer than a normal API call. |
| `TLS_VERIFY` | No | `true` | Verify HTTPS certificates. Set to `false` only for a deliberately trusted endpoint with a self-signed certificate. |
| `LLAMA_SWAP_API_KEY` | No | empty | API key sent as `Authorization: Bearer …`. It is never written to logs. |
| `TRACK_INFLIGHT` | No | `true` | Watch `/api/events` and refuse to swap while another model has an active request. Disabling this can interrupt long generations. |
| `EVENT_RECONNECT_DELAY` | No | `5s` | Delay before reconnecting a disconnected llama-swap event stream. |
| `ACTIVITY_LIMIT` | No | `100` | Number of recent activity entries inspected (`1`–`1000`) when finding the latest request to another model. |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

Boolean values accept `true`/`false`, `yes`/`no`, `on`/`off`, or `1`/`0`.

## How it works

On each check, llama-swap-keeper:

1. Reads `/running` and stops if the preferred model is already starting or
   ready. When a different running-model set is observed, that load time starts
   a fresh idle window—even if no inference request was made.
2. Uses the `/api/events` stream to maintain an in-memory view of in-flight
   requests. If the stream is unavailable or another model is busy, it fails
   safe and does not initiate a swap.
3. Reads recent entries from `/api/metrics/activity` and finds the latest
   completed request to a model other than the preferred model.
4. Once the idle window has elapsed, requests
   `/upstream/<model>/health`, which triggers llama-swap's normal model-loading
   path and verifies that the upstream becomes healthy.

All runtime state is held in memory and reconstructed from llama-swap after a
restart. No volume is needed.

## Logs

The logs are the service's user interface. At `INFO`, the keeper logs startup,
connection changes, policy state changes, successful loads, and errors—but not
every poll.

```text
2026-08-20 20:00:00 INFO starting llama-swap-keeper: url=http://llama-swap:8080 model='gemma-4-26B-A4B-it-qat-UD-Q4_K_XL' idle_timeout=240.0s poll_interval=15.0s tls_verify=True track_inflight=True
2026-08-20 20:00:00 INFO connected to llama-swap event stream
2026-08-20 20:00:00 INFO llama-swap is active or within the 240s idle window; waiting
2026-08-20 20:04:05 INFO idle window elapsed; loading preferred model 'gemma-4-26B-A4B-it-qat-UD-Q4_K_XL'
2026-08-20 20:04:31 INFO preferred model 'gemma-4-26B-A4B-it-qat-UD-Q4_K_XL' loaded successfully
```

View them with:

```bash
docker logs -f llama-swap-keeper
```

## Security notes

- Prefer a private Docker network and do not expose the keeper—it has no
  listening port.
- Use `LLAMA_SWAP_API_KEY` when llama-swap requires authentication.
- Keep `TLS_VERIFY=true` for HTTPS endpoints whenever possible.
- The API key is only held in process memory and is not included in log output.

## Development

The application uses only Python's standard library.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q keeper.py tests
docker build -t llama-swap-keeper:dev .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines and
[SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Releases and container images

Every published GitHub Release triggers a GitHub Actions workflow that builds
and pushes multi-architecture images to GHCR. A release such as `v1.2.3`
produces `1.2.3`, `1.2`, `1`, and `latest` tags (prereleases do not update
`latest`).

## License

[MIT](LICENSE)
