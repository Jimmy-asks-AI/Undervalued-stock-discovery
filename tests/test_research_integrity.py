from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import research_integrity as integrity


class AShareTradingCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = integrity.AShareTradingCalendar(
            ["2026-02-25", "2026-02-13", "2026-02-24", "2026-02-13"],
            source="unit-test",
        )

    def test_strict_session_operations_skip_non_sessions(self) -> None:
        self.assertEqual(self.calendar.dates, (
            date(2026, 2, 13),
            date(2026, 2, 24),
            date(2026, 2, 25),
        ))
        self.assertTrue(self.calendar.is_trading_day("2026-02-13"))
        self.assertFalse(self.calendar.is_trading_day("2026-02-16"))
        self.assertEqual(self.calendar.next_trading_day("2026-02-13"), date(2026, 2, 24))
        self.assertEqual(self.calendar.next_trading_day("2026-02-16"), date(2026, 2, 24))
        self.assertEqual(self.calendar.previous_trading_day("2026-02-24"), date(2026, 2, 13))
        self.assertEqual(self.calendar.roll_forward("2026-02-16"), date(2026, 2, 24))
        self.assertEqual(self.calendar.roll_backward("2026-02-16"), date(2026, 2, 13))
        self.assertEqual(self.calendar.shift("2026-02-13", 2), date(2026, 2, 25))
        self.assertEqual(
            self.calendar.trading_days("2026-02-13", "2026-02-25", inclusive="neither"),
            (date(2026, 2, 24),),
        )

    def test_shift_requires_a_real_exchange_session(self) -> None:
        with self.assertRaises(integrity.TradingCalendarError):
            self.calendar.shift("2026-02-16", 1)
        with self.assertRaises(integrity.TradingCalendarError):
            self.calendar.require_coverage("2026-02-26")

    def test_functional_wrappers_use_explicit_sessions_only(self) -> None:
        sessions = ["2026-02-13", "2026-02-24", "2026-02-25"]
        self.assertEqual(
            integrity.next_trading_day(sessions, "2026-02-13"),
            date(2026, 2, 24),
        )
        self.assertEqual(
            integrity.holding_exit(sessions, "2026-02-13", 2),
            date(2026, 2, 25),
        )
        self.assertEqual(
            self.calendar.holding_exit("2026-02-13", 1),
            date(2026, 2, 24),
        )
        with self.assertRaises(integrity.TradingCalendarError):
            integrity.holding_exit(sessions, "2026-02-16", 1)

    def test_loader_caches_injected_provider_and_reuses_utf8_bom_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "calendar.csv"
            calls = []

            def fetcher():
                calls.append(True)
                return ["2026-01-05", "2026-01-06"]

            first = integrity.load_a_share_trading_calendar(
                cache,
                required_through="2026-01-06",
                fetcher=fetcher,
            )
            second = integrity.load_a_share_trading_calendar(
                cache,
                required_through="2026-01-06",
                fetcher=lambda: self.fail("fresh cache should avoid provider call"),
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(first.dates, second.dates)
            self.assertTrue(cache.read_bytes().startswith(b"\xef\xbb\xbf"))


class FingerprintAndAtomicWriteTests(unittest.TestCase):
    def test_json_fingerprint_is_format_and_key_order_independent(self) -> None:
        left = {"b": [2, 1], "a": {"date": date(2026, 1, 2), "ok": True}}
        right = {"a": {"ok": True, "date": "2026-01-02"}, "b": [2, 1]}
        self.assertEqual(integrity.json_fingerprint(left), integrity.json_fingerprint(right))
        with self.assertRaises(ValueError):
            integrity.json_fingerprint({"bad": float("nan")})

    def test_json_file_fingerprint_ignores_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            first.write_text('{"b":2,"a":1}', encoding="utf-8")
            second.write_text('{\n  "a": 1,\n  "b": 2\n}\n', encoding="utf-8")
            self.assertEqual(
                integrity.fingerprint_json_file(first),
                integrity.fingerprint_json_file(second),
            )

    def test_csv_fingerprint_has_canonical_columns_and_optional_row_sort(self) -> None:
        rows = [{"b": 2, "a": "甲"}, {"a": "乙", "b": 1}]
        reordered_keys = [{"a": "甲", "b": 2}, {"b": 1, "a": "乙"}]
        reversed_rows = list(reversed(reordered_keys))
        self.assertEqual(
            integrity.csv_fingerprint(rows),
            integrity.csv_fingerprint(reordered_keys),
        )
        self.assertNotEqual(
            integrity.csv_fingerprint(rows),
            integrity.csv_fingerprint(reversed_rows),
        )
        self.assertEqual(
            integrity.csv_fingerprint(rows, sort_rows_by=["a"]),
            integrity.csv_fingerprint(reversed_rows, sort_rows_by=["a"]),
        )

    def test_empty_csv_file_fingerprint_retains_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.csv"
            reordered = Path(directory) / "reordered.csv"
            different = Path(directory) / "different.csv"
            first.write_text("a,b\n", encoding="utf-8")
            reordered.write_text("b,a\n", encoding="utf-8")
            different.write_text("a,c\n", encoding="utf-8")
            self.assertEqual(
                integrity.fingerprint_csv_file(first),
                integrity.fingerprint_csv_file(reordered),
            )
            self.assertNotEqual(
                integrity.fingerprint_csv_file(first),
                integrity.fingerprint_csv_file(different),
            )

    def test_atomic_write_replaces_and_preserves_original_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            integrity.atomic_write_json(target, {"value": 1})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"value": 1})
            with mock.patch.object(integrity.os, "replace", side_effect=OSError("injected")):
                with self.assertRaisesRegex(OSError, "injected"):
                    integrity.atomic_write_json(target, {"value": 2})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"value": 1})
            self.assertEqual(list(Path(directory).glob(".state.json.*.tmp")), [])

    def test_file_sha256_hashes_raw_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "payload.bin"
            payload = b"raw\x00bytes\n"
            target.write_bytes(payload)
            self.assertEqual(
                integrity.file_sha256(target, chunk_size=3),
                hashlib.sha256(payload).hexdigest(),
            )
            with self.assertRaises(ValueError):
                integrity.file_sha256(target, chunk_size=0)

    def test_canonical_row_hash_is_order_independent_and_excludes_hash_field(self) -> None:
        left = {"event_id": "e1", "value": 1, "record_hash": "old"}
        right = {"value": 1, "record_hash": "new", "event_id": "e1"}
        self.assertEqual(
            integrity.canonical_row_hash(left),
            integrity.canonical_row_hash(right),
        )


class InterProcessFileLockTests(unittest.TestCase):
    def child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(SCRIPTS) + (os.pathsep + existing if existing else "")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def test_lock_blocks_another_process_and_is_reacquirable(self) -> None:
        child_code = (
            "import sys\n"
            "from research_integrity import InterProcessFileLock\n"
            "with InterProcessFileLock(sys.argv[1], timeout=2):\n"
            " print('locked', flush=True)\n"
            " sys.stdin.readline()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "ledger.lock"
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", child_code, str(lock_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.child_environment(),
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                with self.assertRaises(integrity.LockTimeoutError):
                    with integrity.InterProcessFileLock(lock_path, timeout=0.15, poll_interval=0.02):
                        self.fail("second process acquired a held lock")
                process.stdin.write("release\n")
                process.stdin.flush()
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, msg=stdout + stderr)
                with integrity.InterProcessFileLock(lock_path, timeout=1):
                    self.assertTrue(lock_path.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_operating_system_releases_lock_after_process_crash(self) -> None:
        child_code = (
            "import sys, time\n"
            "from research_integrity import InterProcessFileLock\n"
            "with InterProcessFileLock(sys.argv[1], timeout=2):\n"
            " print('locked', flush=True)\n"
            " time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "ledger.lock"
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", child_code, str(lock_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.child_environment(),
            )
            self.assertEqual(process.stdout.readline().strip(), "locked")
            process.kill()
            process.communicate(timeout=5)
            with integrity.InterProcessFileLock(lock_path, timeout=1):
                self.assertTrue(lock_path.exists())


class HashChainLedgerTests(unittest.TestCase):
    def test_append_verify_unique_key_and_expected_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = integrity.HashChainLedger(Path(directory) / "events.jsonl")
            first = ledger.append({"event_id": "e1", "value": 1}, unique_fields=["event_id"])
            second = ledger.append(
                {"event_id": "e2", "value": 2},
                unique_fields=["event_id"],
                expected_head=first["record_hash"],
            )
            verified = ledger.verify(expected_head=second["record_hash"])
            self.assertEqual(verified.record_count, 2)
            self.assertEqual(verified.records[0]["previous_hash"], integrity.GENESIS_HASH)
            self.assertEqual(verified.records[1]["previous_hash"], first["record_hash"])
            with self.assertRaises(integrity.DuplicateRecordError):
                ledger.append({"event_id": "e2", "value": 3}, unique_fields=["event_id"])
            with self.assertRaises(integrity.HeadMismatchError):
                ledger.verify(expected_head="0" * 64)

    def test_tampering_is_detected_with_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            ledger = integrity.HashChainLedger(path)
            ledger.append({"event_id": "e1", "value": 1})
            ledger.append({"event_id": "e2", "value": 2})
            lines = path.read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[0])
            record["value"] = 99
            lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(integrity.HashChainError) as caught:
                ledger.verify()
            self.assertEqual(caught.exception.line_number, 1)

    def test_append_is_atomic_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            ledger = integrity.HashChainLedger(path)
            ledger.append({"event_id": "e1"})
            head = ledger.verify().head_hash
            before = path.read_bytes()
            with mock.patch.object(integrity.os, "replace", side_effect=OSError("injected")):
                with self.assertRaisesRegex(OSError, "injected"):
                    ledger.append({"event_id": "e2"})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(ledger.verify(expected_head=head).record_count, 1)

    def test_concurrent_process_appends_are_serialized(self) -> None:
        child_code = (
            "import sys\n"
            "from research_integrity import HashChainLedger\n"
            "HashChainLedger(sys.argv[1], lock_timeout=5).append({'event_id': sys.argv[2]}, unique_fields=['event_id'])\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            environment = os.environ.copy()
            existing = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = str(SCRIPTS) + (os.pathsep + existing if existing else "")
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            processes = [
                subprocess.Popen(
                    [sys.executable, "-B", "-c", child_code, str(path), f"e{index}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                for index in range(6)
            ]
            failures = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                if process.returncode:
                    failures.append(stdout + stderr)
            self.assertEqual(failures, [])
            verified = integrity.verify_hash_chain(path)
            self.assertEqual(verified.record_count, 6)
            self.assertEqual({row["event_id"] for row in verified.records}, {f"e{i}" for i in range(6)})


class CsvHashChainLedgerTests(unittest.TestCase):
    def test_append_preserves_old_prefix_and_verifies_path_and_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            first_batch = integrity.append_hash_chained_csv(
                path,
                [
                    {"event_id": "e1", "value": 1},
                    {"event_id": "e2", "value": 2},
                ],
                ["event_id", "value"],
            )
            old_prefix = path.read_bytes()
            second_batch = integrity.append_hash_chained_csv(
                path,
                {"event_id": "e3", "value": 3},
                ["event_id", "value"],
                expected_head=first_batch[-1]["record_hash"],
            )
            self.assertTrue(path.read_bytes().startswith(old_prefix))
            verified_path = integrity.verify_hash_chain(
                path,
                expected_head=second_batch[-1]["record_hash"],
            )
            self.assertEqual(verified_path.record_count, 3)
            self.assertEqual(
                list(verified_path.records[0]),
                ["event_id", "value", "previous_hash", "record_hash"],
            )
            self.assertEqual(
                integrity.verify_hash_chain(verified_path.records).head_hash,
                verified_path.head_hash,
            )

    def test_duplicate_id_is_rejected_without_mutating_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            integrity.append_hash_chained_csv(
                path,
                {"event_id": "e1", "value": 1},
                ["event_id", "value"],
            )
            before = path.read_bytes()
            with self.assertRaises(integrity.DuplicateRecordError):
                integrity.append_hash_chained_csv(
                    path,
                    {"event_id": "e1", "value": 2},
                    ["event_id", "value"],
                )
            self.assertEqual(path.read_bytes(), before)

    def test_interruption_before_replace_preserves_file_and_removes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.csv"
            integrity.append_hash_chained_csv(
                path,
                {"event_id": "e1", "value": 1},
                ["event_id", "value"],
            )
            before = path.read_bytes()
            with mock.patch.object(integrity.os, "replace", side_effect=OSError("injected")):
                with self.assertRaisesRegex(OSError, "injected"):
                    integrity.append_hash_chained_csv(
                        path,
                        {"event_id": "e2", "value": 2},
                        ["event_id", "value"],
                    )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob(".events.csv.*.tmp")), [])
            self.assertEqual(integrity.verify_hash_chain(path).record_count, 1)


if __name__ == "__main__":
    unittest.main()
