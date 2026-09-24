"""Offline regression fixtures for the incremental agent.log cursor."""
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "logs_incremental",
    Path(__file__).resolve().parents[1] / "scripts/aikub_telemetry_logs_incremental.py",
)
logs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logs)


class LogTimestampTests(unittest.TestCase):
    def test_utc_override_normalizes_and_preserves_redacted_raw(self):
        raw = "2026-07-01 12:34:56,123 INFO test: token=private"
        with patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": "UTC"}):
            entry = logs.parse_entry(1, raw)
        self.assertEqual(entry["timestamp"], "2026-07-01T12:34:56.123Z")
        self.assertEqual(entry["sourceTimezone"], "UTC")
        self.assertEqual(entry["timezoneSource"], "AIKUB_LOG_TIMEZONE")
        self.assertEqual(entry["timestampStatus"], "normalized")
        self.assertEqual(entry["raw"], raw.replace("private", "[REDACTED]"))


    def test_toronto_dst_is_resolved_only_when_unique(self):
        cases = [
            ("2026-07-01 12:00:00,123", "2026-07-01T16:00:00.123Z", "normalized"),
            ("2026-01-01 12:00:00,123", "2026-01-01T17:00:00.123Z", "normalized"),
            ("2026-11-01 01:30:00,123", None, "ambiguous_local_time"),
            ("2026-03-08 02:30:00,123", None, "nonexistent_local_time"),
        ]
        with patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": "America/Toronto"}):
            for stamp, expected, status in cases:
                with self.subTest(stamp=stamp):
                    entry = logs.parse_entry(1, stamp + " INFO test: hi")
                    self.assertEqual(entry["timestamp"], expected)
                    self.assertEqual(entry["timestampStatus"], status)
                    self.assertEqual(entry["sourceTimezone"], "America/Toronto")


    def test_explicit_offsets_are_authoritative_and_auto_detected(self):
        cases = [
            ("2026-07-01T12:00:00.123Z", "2026-07-01T12:00:00.123Z", "UTC"),
            ("2026-07-01 12:00:00,123-04:00", "2026-07-01T16:00:00.123Z", "-04:00"),
            ("2026-01-01T12:00:00+0530", "2026-01-01T06:30:00.000Z", "+05:30"),
            ("2026-11-01T01:30:00-0500", "2026-11-01T06:30:00.000Z", "-05:00"),
        ]
        for override in ("", "America/Toronto"):
            with patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": override}):
                for stamp, expected, zone in cases:
                    with self.subTest(stamp=stamp, override=override):
                        entry = logs.parse_entry(1, stamp + " INFO test: hi")
                        self.assertEqual(entry["timestamp"], expected)
                        self.assertEqual(entry["sourceTimezone"], zone)
                        self.assertEqual(entry["timezoneSource"], "explicit_offset")
                        self.assertEqual(logs.parse_ts(stamp + " INFO hi"), datetime.fromisoformat(expected))


    def test_unknown_source_and_invalid_dates_never_invent_utc(self):
        with patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": "", "TZ": "UTC"}):
            entry = logs.parse_entry(1, "2026-07-01 12:00:00,123 INFO test: hi")
            self.assertIsNone(entry["timestamp"])
            self.assertIsNone(entry["sourceTimezone"])
            self.assertEqual(entry["timestampStatus"], "unknown_timezone")
            invalid = logs.parse_entry(2, "2026-02-30 12:00:00,123 INFO test: bad date")
            self.assertIsNone(invalid["timestamp"])
            self.assertEqual(invalid["timestampStatus"], "invalid_timestamp")

    def test_override_requires_valid_iana_zone(self):
        for name in ("Not/AZone", "+04:00", "/etc/localtime", "../UTC", "localtime", "posixrules"):
            with self.subTest(name=name), patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": name}):
                with self.assertRaisesRegex(ValueError, "AIKUB_LOG_TIMEZONE.*IANA"):
                    logs.parse_entry(1, "2026-07-01 12:00:00,123 INFO hi")

    def test_fractional_precision_is_not_lost(self):
        entry = logs.parse_entry(1, "2026-07-01T12:00:00.123456Z INFO hi")
        self.assertEqual(entry["timestamp"], "2026-07-01T12:00:00.123456Z")


class LogCursorTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": "UTC"})
        env.start()
        self.addCleanup(env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "agent.log"
        self.state_path = Path(self.temp.name) / "state.json"
        for name, value in (("LOG_PATH", self.path), ("STATE_PATH", self.state_path)):
            patcher = patch.object(logs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path.write_text("", encoding="utf-8")

    def line(self, message="recent", days=0):
        stamp = datetime.now(timezone.utc) - timedelta(days=days)
        return stamp.strftime("%Y-%m-%d %H:%M:%S,000") + f" INFO test: {message}\n"

    def collect(self, baseline_line=None):
        entries, state = logs.collect_lines(3, baseline_line)
        logs.write_state(state)
        return entries, state

    def test_first_run_excludes_known_old_but_keeps_unknown_orphans(self):
        self.path.write_text(
            "orphan\n" + self.line("old", days=4) + "old traceback\n"
            + self.line() + "recent traceback\n", encoding="utf-8",
        )
        entries, state = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [1, 4, 5])
        self.assertIsNone(entries[0]["parentTimestamp"])
        self.assertEqual(entries[2]["parentLine"], 4)
        self.assertEqual(state["lastLine"], 5)
        self.assertEqual(state["entriesInThisRun"], 3)
        self.assertEqual(state["levelsInThisRun"], {"INFO": 1})

    def test_rotation_keeps_global_numbers_and_parent_references(self):
        self.path.write_text(self.line() * 3, encoding="utf-8")
        self.collect()
        self.path.rename(self.path.with_suffix(".log.1"))
        self.path.write_text(self.line("rotated") + "traceback\n", encoding="utf-8")
        entries, state = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [4, 5])
        self.assertEqual(entries[1]["parentLine"], 4)
        self.assertEqual(state["lastLine"], 5)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(self.line("appended"))
        entries, state = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [6])
        self.assertEqual(state["lastLine"], 6)

    def test_append_during_read_does_not_replay_consumed_bytes(self):
        self.path.write_text(self.line("initial"), encoding="utf-8")
        original = logs.parse_entry

        def append_once(*args, **kwargs):
            if args[0] == 1:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(self.line("concurrent é"))
            return original(*args, **kwargs)

        with patch.object(logs, "parse_entry", side_effect=append_once):
            first, state = self.collect()
        second, state = self.collect()
        self.assertEqual([entry["message"] for entry in first + second],
                         ["initial", "concurrent é"])
        self.assertEqual(state["offset"], self.path.stat().st_size)
        self.assertEqual(state["lastLine"], 2)

    def test_append_and_repeated_empty_runs_keep_exact_count(self):
        self.path.write_text(self.line() + "traceback\n", encoding="utf-8")
        entries, initial = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [1, 2])
        for _ in range(3):
            entries, state = self.collect()
            self.assertEqual(entries, [])
            self.assertEqual(state["lastLine"], 2)
            self.assertEqual(state["offset"], initial["offset"])
            self.assertEqual(state["entriesInThisRun"], 0)
            self.assertEqual(state["levelsInThisRun"], {})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(self.line("new"))
        entries, state = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [3])
        self.assertEqual(state["lastLine"], 3)

    def test_truncate_same_inode_keeps_global_number(self):
        self.path.write_text(self.line() * 4, encoding="utf-8")
        _, before = self.collect()
        self.path.write_text(self.line("truncated"), encoding="utf-8")
        entries, state = self.collect()
        self.assertEqual(before["inode"], state["inode"])
        self.assertEqual([entry["line"] for entry in entries], [5])
        self.assertEqual(state["lastLine"], 5)
        self.assertEqual(state["offset"], self.path.stat().st_size)

    def test_empty_rotated_file_does_not_reset_high_water_mark(self):
        self.path.write_text(self.line() * 3, encoding="utf-8")
        self.collect()
        self.path.rename(self.path.with_suffix(".log.1"))
        self.path.write_text("", encoding="utf-8")
        for _ in range(2):
            entries, state = self.collect()
            self.assertEqual(entries, [])
            self.assertEqual(state["lastLine"], 3)
            self.assertEqual(state["offset"], 0)
        self.path.write_text(self.line(), encoding="utf-8")
        entries, _ = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [4])

    def test_unknown_times_and_their_continuations_survive_first_run(self):
        self.path.write_text("2000-01-01 00:00:00,000 INFO test: unknown\ntraceback\n")
        with patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": ""}):
            entries, state = self.collect()
        self.assertEqual(len(entries), 2)
        self.assertIsNone(entries[0]["timestamp"])
        self.assertIsNone(entries[1]["parentTimestamp"])
        self.assertEqual(entries[1]["parentTimestampStatus"], "unknown_timezone")
        self.assertEqual(state["sourceTimezone"], None)

    def test_filter_and_continuation_use_normalized_parent_including_next_run(self):
        fixed_now = datetime(2026, 7, 4, 15, tzinfo=timezone.utc)
        self.path.write_text(
            "2026-07-01 12:00:00,000 INFO test: keep\ntraceback\n"
            "2026-06-01 12:00:00,000 INFO test: old\nold traceback\n"
            "2026-07-01 13:00:00,000 ERROR test: newest\n")
        with patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": "America/Toronto"}), patch.object(logs, "datetime", wraps=datetime) as clock:
            clock.now.return_value = fixed_now
            entries, state = self.collect()
            self.assertEqual([e["line"] for e in entries], [1, 2, 5])
            self.assertEqual(entries[1]["parentTimestamp"], "2026-07-01T16:00:00.000Z")
            self.assertEqual(entries[1]["parentSourceTimezone"], "America/Toronto")
            self.assertEqual(state["sourceTimezone"], "America/Toronto")
            with self.path.open("a") as handle:
                handle.write("next-run traceback\n")
            follow, _ = self.collect()
            self.assertEqual(follow[0]["parentLine"], 5)
            self.assertEqual(follow[0]["parentTimestamp"], "2026-07-01T17:00:00.000Z")

    def test_payload_transmits_source_metadata_without_altering_raw(self):
        self.path.write_text("2026-07-01T12:00:00-04:00 INFO test: token=secret\ntrace\n")
        entries, state = logs.collect_lines(3, baseline_line=0)
        event = logs.build_log_payload({"payload": {}}, "fixture", "bot", None,
                                       "hermes", state, entries, 1, 1)
        payload = event["payload"]["logs"]
        self.assertEqual(payload["timestampFormat"], "UTC_ISO8601_Z_or_null")
        self.assertEqual(payload["sourceTimezone"], "UTC")  # configured fallback only
        self.assertEqual(payload["items"][0]["sourceTimezone"], "-04:00")
        self.assertEqual(payload["items"][1]["parentSourceTimezone"], "-04:00")
        self.assertEqual(payload["items"][0]["raw"],
                         "2026-07-01T12:00:00-04:00 INFO test: token=[REDACTED]")
        self.assertEqual(payload["items"][0]["timestamp"], "2026-07-01T16:00:00.000Z")
        self.assertNotIn("raw", state["lastParent"])
        self.assertFalse(self.state_path.exists())  # collecting alone never commits cursor
        self.assertEqual(event["eventType"], "bot_inventory_snapshot")
        self.assertTrue(event["occurredAt"].endswith("Z"))

    def test_invalid_zone_fails_even_for_empty_file_without_writing_state(self):
        with patch.dict(logs.os.environ, {"AIKUB_LOG_TIMEZONE": "Invalid/Zone"}):
            with self.assertRaisesRegex(ValueError, "AIKUB_LOG_TIMEZONE"):
                logs.collect_lines(3)
        self.assertFalse(self.state_path.exists())

    def test_initial_empty_file(self):
        for _ in range(2):
            entries, state = self.collect()
            self.assertEqual(entries, [])
            self.assertEqual(state["lastLine"], 0)
            self.assertEqual(state["offset"], 0)
        self.path.write_text(self.line(), encoding="utf-8")
        entries, _ = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [1])

    def test_old_only_first_run_counts_filtered_lines(self):
        self.path.write_text(self.line(days=4) + "old continuation\n", encoding="utf-8")
        entries, state = self.collect()
        self.assertEqual(entries, [])
        self.assertEqual(state["lastLine"], 2)
        entries, state = self.collect()
        self.assertEqual(entries, [])
        self.assertEqual(state["lastLine"], 2)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(self.line())
        entries, _ = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [3])

    def test_legacy_state_resumes_and_then_rotates(self):
        self.path.write_text(self.line() * 2, encoding="utf-8")
        stat = self.path.stat()
        logs.write_state({"inode": stat.st_ino, "offset": stat.st_size, "lastLine": 42})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(self.line("new"))
        entries, state = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [43])
        self.assertEqual(state["mode"], "incremental_since_cursor")
        self.path.rename(self.path.with_suffix(".log.1"))
        self.path.write_text(self.line("rotated"), encoding="utf-8")
        entries, _ = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [44])

    def test_manual_baseline_still_skips_physical_lines(self):
        self.path.write_text(self.line() * 3, encoding="utf-8")
        entries, state = self.collect(baseline_line=2)
        self.assertEqual([entry["line"] for entry in entries], [3])
        self.assertEqual(state["lastLine"], 3)
        self.assertEqual(state["mode"], "manual_baseline_line_no_duplicates")

    def test_filtered_trailing_line_is_counted(self):
        self.path.write_text(self.line() + self.line(days=4), encoding="utf-8")
        entries, state = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [1])
        self.assertEqual(state["lastLine"], 2)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(self.line())
        entries, _ = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [3])


if __name__ == "__main__":
    unittest.main()
