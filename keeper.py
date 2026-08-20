#!/usr/bin/env python3
"""Keep a preferred llama-swap model warm when the server is idle."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

LOG = logging.getLogger("llama-swap-keeper")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)


def parse_duration(value: str) -> float:
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"invalid duration: {value!r} (use ms, s, m, or h)")
    number = float(match.group(1))
    factor = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match.group(2).lower() if match.group(2) else "s"]
    return number * factor


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


@dataclass(frozen=True)
class Config:
    model: str
    url: str = "http://localhost:8080"
    idle_timeout: float = 240
    poll_interval: float = 15
    request_timeout: float = 30
    load_timeout: float = 900
    tls_verify: bool = True
    api_key: str = ""
    track_inflight: bool = True
    event_reconnect_delay: float = 5
    activity_limit: int = 100
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        model = os.getenv("LLAMA_SWAP_MODEL", "").strip()
        if not model:
            raise ValueError("LLAMA_SWAP_MODEL is required")
        activity_limit = int(os.getenv("ACTIVITY_LIMIT", "100"))
        if activity_limit < 1 or activity_limit > 1000:
            raise ValueError("ACTIVITY_LIMIT must be between 1 and 1000")
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")
        return cls(
            model=model,
            url=os.getenv("LLAMA_SWAP_URL", "http://localhost:8080").strip().rstrip("/"),
            idle_timeout=parse_duration(os.getenv("IDLE_TIMEOUT", "4m")),
            poll_interval=parse_duration(os.getenv("POLL_INTERVAL", "15s")),
            request_timeout=parse_duration(os.getenv("REQUEST_TIMEOUT", "30s")),
            load_timeout=parse_duration(os.getenv("LOAD_TIMEOUT", "15m")),
            tls_verify=parse_bool(os.getenv("TLS_VERIFY", "true")),
            api_key=os.getenv("LLAMA_SWAP_API_KEY", ""),
            track_inflight=parse_bool(os.getenv("TRACK_INFLIGHT", "true")),
            event_reconnect_delay=parse_duration(os.getenv("EVENT_RECONNECT_DELAY", "5s")),
            activity_limit=activity_limit,
            log_level=log_level,
        )


class Decision(Enum):
    TARGET_RUNNING = "target_running"
    INFLIGHT = "inflight"
    INFLIGHT_UNKNOWN = "inflight_unknown"
    RECENT_ACTIVITY = "recent_activity"
    LOAD = "load"


class InflightTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, dict[str, Any]] = {}
        self.ready = False

    def apply(self, update: dict[str, Any]) -> None:
        operation = update.get("operation")
        with self._lock:
            if operation == "snapshot":
                self._requests = {str(item["id"]): item for item in update.get("requests", []) if item.get("id")}
                self.ready = True
            elif operation == "upsert" and update.get("request", {}).get("id"):
                item = update["request"]
                self._requests[str(item["id"])] = item
            elif operation == "remove":
                self._requests.pop(str(update.get("id", "")), None)

    def mark_unknown(self) -> None:
        with self._lock:
            self.ready = False

    def snapshot(self) -> tuple[bool, list[dict[str, Any]]]:
        with self._lock:
            return self.ready, list(self._requests.values())

    def has_other(self, model: str) -> bool:
        with self._lock:
            return any(item.get("model") != model for item in self._requests.values())


class LlamaSwapClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.ssl_context = ssl.create_default_context() if config.tls_verify else ssl._create_unverified_context()

    def _request(self, path: str, timeout: float | None = None) -> urllib.response.addinfourl:
        headers = {"Accept": "application/json", "User-Agent": "llama-swap-keeper/0.1"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(f"{self.config.url}{path}", headers=headers)
        return urllib.request.urlopen(
            request,
            timeout=timeout if timeout is not None else self.config.request_timeout,
            context=self.ssl_context,
        )

    def get_json(self, path: str) -> dict[str, Any]:
        with self._request(path) as response:
            return json.load(response)

    def running(self) -> list[dict[str, Any]]:
        return self.get_json("/running").get("running", [])

    def activities(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"limit": self.config.activity_limit, "page": 1, "sort": "time", "order": "desc"}
        )
        return self.get_json(f"/api/metrics/activity?{query}").get("data", [])

    def load(self) -> None:
        model = urllib.parse.quote(self.config.model, safe="")
        cache_buster = time.time_ns()
        with self._request(f"/upstream/{model}/health?_={cache_buster}", timeout=self.config.load_timeout) as response:
            if response.status >= 400:
                raise RuntimeError(f"load request returned HTTP {response.status}")
            response.read(1024)

    def event_lines(self):
        with self._request("/api/events", timeout=max(self.config.request_timeout, 600)) as response:
            for raw_line in response:
                yield raw_line.decode("utf-8", errors="replace").rstrip("\r\n")


class EventWatcher(threading.Thread):
    def __init__(self, client: LlamaSwapClient, tracker: InflightTracker, stop_event: threading.Event) -> None:
        super().__init__(name="llama-swap-events", daemon=True)
        self.client = client
        self.tracker = tracker
        self.stop_event = stop_event
        self._connected_once = False

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._consume()
            except Exception as exc:  # network errors are retried; main loop fails safe
                self.tracker.mark_unknown()
                LOG.warning("llama-swap event stream disconnected: %s; retrying in %ss", exc, self.client.config.event_reconnect_delay)
            if self.stop_event.wait(self.client.config.event_reconnect_delay):
                break

    def _consume(self) -> None:
        if self._connected_once:
            LOG.info("reconnected to llama-swap event stream")
        else:
            LOG.info("connected to llama-swap event stream")
            self._connected_once = True
        for line in self.client.event_lines():
            if self.stop_event.is_set():
                return
            if not line.startswith("data:"):
                continue
            envelope = json.loads(line[5:])
            if envelope.get("type") != "inflight":
                continue
            self.tracker.apply(json.loads(envelope["data"]))


class Monitor:
    def __init__(self, config: Config, client: LlamaSwapClient | None = None) -> None:
        self.config = config
        self.client = client or LlamaSwapClient(config)
        self.tracker = InflightTracker()
        if not config.track_inflight:
            self.tracker.ready = True
        self.started_at = datetime.now(timezone.utc)
        self._last_decision: Decision | None = None

    def decide(
        self,
        running: list[dict[str, Any]],
        activities: list[dict[str, Any]],
        now: datetime,
        inflight_ready: bool,
        has_other_inflight: bool,
    ) -> Decision:
        if any(item.get("model") == self.config.model and item.get("state") != "stopped" for item in running):
            return Decision.TARGET_RUNNING
        if self.config.track_inflight and not inflight_ready:
            return Decision.INFLIGHT_UNKNOWN
        if has_other_inflight:
            return Decision.INFLIGHT

        latest_other: datetime | None = None
        for item in activities:
            if item.get("model") == self.config.model:
                continue
            raw = item.get("timestamp")
            if not raw:
                continue
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if latest_other is None or parsed > latest_other:
                latest_other = parsed

        idle_since = latest_other or self.started_at
        if (now - idle_since).total_seconds() < self.config.idle_timeout:
            return Decision.RECENT_ACTIVITY
        return Decision.LOAD

    def _log_decision(self, decision: Decision, activities: list[dict[str, Any]]) -> None:
        if decision == self._last_decision:
            return
        messages = {
            Decision.TARGET_RUNNING: f"preferred model {self.config.model!r} is running; monitoring quietly",
            Decision.INFLIGHT: "another model is serving an in-flight request; waiting",
            Decision.INFLIGHT_UNKNOWN: "waiting for in-flight request state before taking action",
            Decision.RECENT_ACTIVITY: f"llama-swap is active or within the {self.config.idle_timeout:g}s idle window; waiting",
            Decision.LOAD: f"idle window elapsed; loading preferred model {self.config.model!r}",
        }
        LOG.info(messages[decision])
        self._last_decision = decision

    def check_once(self) -> Decision:
        running = self.client.running()
        activities = self.client.activities()
        ready, requests = self.tracker.snapshot()
        has_other = any(item.get("model") != self.config.model for item in requests)
        decision = self.decide(running, activities, datetime.now(timezone.utc), ready, has_other)
        self._log_decision(decision, activities)
        if decision == Decision.LOAD:
            self.client.load()
            LOG.info("preferred model %r loaded successfully", self.config.model)
            self._last_decision = Decision.TARGET_RUNNING
        return decision

    def run(self, stop_event: threading.Event) -> None:
        watcher: EventWatcher | None = None
        if self.config.track_inflight:
            watcher = EventWatcher(self.client, self.tracker, stop_event)
            watcher.start()
        while not stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:
                LOG.error("monitor check failed: %s", exc)
                self._last_decision = None
            stop_event.wait(self.config.poll_interval)


def main() -> int:
    try:
        config = Config.from_env()
    except (ValueError, TypeError) as exc:
        logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
        LOG.error("configuration error: %s", exc)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOG.info(
        "starting llama-swap-keeper: url=%s model=%r idle_timeout=%ss poll_interval=%ss tls_verify=%s track_inflight=%s",
        config.url,
        config.model,
        config.idle_timeout,
        config.poll_interval,
        config.tls_verify,
        config.track_inflight,
    )
    stop_event = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _signum, _frame: stop_event.set())
    Monitor(config).run(stop_event)
    LOG.info("stopped llama-swap-keeper")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
