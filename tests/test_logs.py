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


class LogCursorTests(unittest.TestCase):
    def setUp(self):
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

    def test_first_run_excludes_old_and_orphan_continuations(self):
        self.path.write_text(
            "orphan\n" + self.line("old", days=4) + "old traceback\n"
            + self.line() + "recent traceback\n", encoding="utf-8",
        )
        entries, state = self.collect()
        self.assertEqual([entry["line"] for entry in entries], [4, 5])
        self.assertEqual(entries[1]["parentLine"], 4)
        self.assertEqual(state["lastLine"], 5)
        self.assertEqual(state["entriesInThisRun"], 2)
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
