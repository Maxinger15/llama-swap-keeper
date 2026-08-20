import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from keeper import Config, Decision, InflightTracker, LlamaSwapClient, Monitor, parse_duration


class DurationTests(unittest.TestCase):
    def test_parses_human_friendly_durations(self):
        self.assertEqual(parse_duration("250ms"), 0.25)
        self.assertEqual(parse_duration("4m"), 240)
        self.assertEqual(parse_duration("1.5h"), 5400)

    def test_rejects_invalid_duration(self):
        with self.assertRaisesRegex(ValueError, "duration"):
            parse_duration("soon")


class ConfigTests(unittest.TestCase):
    def test_defaults_are_documented_runtime_defaults(self):
        with patch.dict(os.environ, {"LLAMA_SWAP_MODEL": "gemma"}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.url, "http://localhost:8080")
        self.assertEqual(cfg.idle_timeout, 240)
        self.assertEqual(cfg.poll_interval, 15)
        self.assertEqual(cfg.request_timeout, 30)
        self.assertEqual(cfg.load_timeout, 900)
        self.assertTrue(cfg.tls_verify)
        self.assertTrue(cfg.track_inflight)

    def test_model_is_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "LLAMA_SWAP_MODEL"):
                Config.from_env()


class InflightTrackerTests(unittest.TestCase):
    def test_applies_snapshot_upsert_and_remove(self):
        tracker = InflightTracker()
        tracker.apply({"operation": "snapshot", "requests": [{"id": "a", "model": "other"}]})
        self.assertTrue(tracker.has_other("gemma"))
        tracker.apply({"operation": "upsert", "request": {"id": "b", "model": "gemma"}})
        tracker.apply({"operation": "remove", "id": "a"})
        self.assertFalse(tracker.has_other("gemma"))
        self.assertTrue(tracker.ready)


class ClientTests(unittest.TestCase):
    def test_load_uses_upstream_health_endpoint(self):
        client = LlamaSwapClient(Config(model="gemma"))
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch.object(client, "_request", return_value=response) as request:
            client.load()
        path = request.call_args.args[0]
        self.assertTrue(path.startswith("/upstream/gemma/health?_="), path)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(model="gemma")
        self.monitor = Monitor(self.cfg)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_keeps_loaded_target_without_action(self):
        decision = self.monitor.decide(
            running=[{"model": "gemma", "state": "ready"}],
            activities=[],
            now=self.now,
            inflight_ready=True,
            has_other_inflight=False,
        )
        self.assertEqual(decision, Decision.TARGET_RUNNING)

    def test_waits_while_another_model_request_is_inflight(self):
        decision = self.monitor.decide([], [], self.now, True, True)
        self.assertEqual(decision, Decision.INFLIGHT)

    def test_loads_after_other_model_has_been_idle_for_timeout(self):
        activities = [{"model": "other", "timestamp": (self.now - timedelta(seconds=241)).isoformat()}]
        decision = self.monitor.decide([], activities, self.now, True, False)
        self.assertEqual(decision, Decision.LOAD)

    def test_waits_until_idle_timeout_has_elapsed(self):
        activities = [{"model": "other", "timestamp": (self.now - timedelta(seconds=239)).isoformat()}]
        decision = self.monitor.decide([], activities, self.now, True, False)
        self.assertEqual(decision, Decision.RECENT_ACTIVITY)

    def test_ignores_target_activity_when_looking_for_other_models(self):
        activities = [
            {"model": "gemma", "timestamp": (self.now - timedelta(seconds=10)).isoformat()},
            {"model": "other", "timestamp": (self.now - timedelta(seconds=500)).isoformat()},
        ]
        decision = self.monitor.decide([], activities, self.now, True, False)
        self.assertEqual(decision, Decision.LOAD)

    def test_fails_safe_until_inflight_snapshot_arrives(self):
        decision = self.monitor.decide([], [], self.now, False, False)
        self.assertEqual(decision, Decision.INFLIGHT_UNKNOWN)

    def test_without_activity_waits_from_process_start(self):
        self.monitor.started_at = self.now - timedelta(seconds=239)
        self.assertEqual(
            self.monitor.decide([], [], self.now, True, False),
            Decision.RECENT_ACTIVITY,
        )
        self.monitor.started_at = self.now - timedelta(seconds=241)
        self.assertEqual(self.monitor.decide([], [], self.now, True, False), Decision.LOAD)


if __name__ == "__main__":
    unittest.main()
