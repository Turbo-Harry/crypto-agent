"""Strict market collection: finality, UPSERT, audit and failure semantics."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from data import collect, upload
from data.market_data import (BAR_MS, audit_window, connect, parse_okx_rows,
                              sync_source_gaps, upsert_rows)


def _row(ts, *, close="1.5", confirm="1", high="2", low="0.5",
         volume="10"):
    return [str(ts), "1", high, low, close, volume, "15", "0", confirm]


class DataCollectionQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "market.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parser_keeps_only_closed_valid_swap_rows(self):
        now = 1_800_000_000_000
        closed_ts = now - 2 * BAR_MS["1m"]
        open_ts = now - BAR_MS["1m"] // 2
        parsed, stats = parse_okx_rows(
            "BTC-USDT-SWAP", "1m", [
                _row(closed_ts),
                _row(closed_ts - BAR_MS["1m"], confirm="0"),
                _row(open_ts),
                _row(closed_ts - 2 * BAR_MS["1m"], high="0.9"),
            ], as_of_ms=now)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(stats["unconfirmed"], 2)
        self.assertEqual(stats["invalid"], 1)
        self.assertEqual(parsed[0]["confirmed"], 1)
        self.assertEqual(parsed[0]["venue"], "swap")
        with self.assertRaisesRegex(ValueError, "SWAP"):
            parse_okx_rows("BTC-USDT", "1m", [_row(closed_ts)],
                           as_of_ms=now)

    def test_final_value_upsert_overwrites_and_is_idempotent(self):
        now = 1_800_000_000_000
        ts = now - 2 * BAR_MS["15m"]
        first, _ = parse_okx_rows(
            "BTC-USDT-SWAP", "15m", [_row(ts, close="1.2", volume="2")],
            as_of_ms=now)
        final, _ = parse_okx_rows(
            "BTC-USDT-SWAP", "15m", [_row(ts, close="1.8", volume="20")],
            as_of_ms=now)
        conn = connect(self.db)
        self.assertEqual(upsert_rows(conn, first)["inserted"], 1)
        result = upsert_rows(conn, final)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(upsert_rows(conn, final)["unchanged"], 1)
        row = conn.execute(
            "SELECT close,volume,confirmed,source,venue,time_zone FROM klines_v2").fetchone()
        self.assertEqual((row["close"], row["volume"]), (1.8, 20.0))
        self.assertEqual(
            (row["confirmed"], row["source"], row["venue"], row["time_zone"]),
            (1, "okx", "swap", "UTC"))
        statuses = dict(conn.execute(
            "SELECT name,status FROM market_datasets").fetchall())
        self.assertEqual(statuses["klines"], "legacy_unverified")
        self.assertEqual(statuses["klines_v2"], "strict_confirmed")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM klines_v2").fetchone()[0], 1)
        conn.close()

    def test_window_audit_detects_exact_gap(self):
        now = 1_800_000_000_000
        start = now - 3 * BAR_MS["1m"]
        rows, _ = parse_okx_rows(
            "BTC-USDT-SWAP", "1m", [
                _row(start), _row(start + 2 * BAR_MS["1m"]),
            ], as_of_ms=now, start_ms=start, end_ms=now)
        conn = connect(self.db)
        upsert_rows(conn, rows)
        report = audit_window(
            conn, ["BTC-USDT-SWAP"], ["1m"], start, now)
        self.assertFalse(report["complete"])
        self.assertEqual(report["missing"], 1)
        more, _ = parse_okx_rows(
            "BTC-USDT-SWAP", "1m", [_row(start + BAR_MS["1m"])],
            as_of_ms=now, start_ms=start, end_ms=now)
        upsert_rows(conn, more)
        self.assertTrue(audit_window(
            conn, ["BTC-USDT-SWAP"], ["1m"], start, now)["complete"])
        conn.close()

    def test_rechecked_source_gap_is_explicit_not_filled(self):
        now = 1_800_000_000_000
        start = now - 2 * BAR_MS["1m"]
        rows, _ = parse_okx_rows(
            "NVDA-USDT-SWAP", "1m", [_row(start)], as_of_ms=now,
            start_ms=start, end_ms=now)
        conn = connect(self.db)
        upsert_rows(conn, rows)
        gap_ts = start + BAR_MS["1m"]
        sync_source_gaps(
            conn, "NVDA-USDT-SWAP", "1m", start, now, [gap_ts],
            checked_at=123.0)
        report = audit_window(
            conn, ["NVDA-USDT-SWAP"], ["1m"], start, now)
        self.assertTrue(report["complete"])
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["source_gaps"], 1)
        self.assertEqual(report["unexplained_missing"], 0)
        self.assertEqual(tuple(conn.execute(
            "SELECT reason,check_count FROM market_data_gaps").fetchone()),
            ("absent_from_okx_history_after_recheck", 1))
        recovered, _ = parse_okx_rows(
            "NVDA-USDT-SWAP", "1m", [_row(gap_ts)], as_of_ms=now,
            start_ms=start, end_ms=now)
        upsert_rows(conn, recovered)
        sync_source_gaps(
            conn, "NVDA-USDT-SWAP", "1m", start, now, [gap_ts],
            checked_at=124.0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM market_data_gaps").fetchone()[0], 0)
        conn.close()

    def test_history_window_pages_and_clips(self):
        start = 1_800_000_000_000
        end = start + 3 * BAR_MS["1m"]
        pages = [
            {"code": "0", "data": [
                _row(start + 2 * BAR_MS["1m"]),
                _row(start + BAR_MS["1m"])]},
            {"code": "0", "data": [
                _row(start), _row(start - BAR_MS["1m"])]},
        ]
        with patch.object(collect, "_get", side_effect=pages):
            rows, requests = collect.fetch_history_window(
                "BTC-USDT-SWAP", "1m", start, end)
        self.assertEqual(requests, 2)
        self.assertEqual([int(row[0]) for row in rows],
                         [start, start + BAR_MS["1m"],
                          start + 2 * BAR_MS["1m"]])

    def test_cli_returns_failure_when_only_open_bar_arrives(self):
        now = 1_800_000_000_000
        with patch.object(collect, "_symbols", return_value=["BTC-USDT-SWAP"]), \
                patch.object(collect, "fetch_recent",
                             return_value=([_row(now, confirm="0")], 1)), \
                patch("data.market_data.time.time", return_value=now / 1000), \
                patch("data.collect.time.time", return_value=now / 1000):
            code = collect.main([
                "--bar", "1m", "--inst", "BTC-USDT-SWAP", "--db", self.db])
        self.assertEqual(code, 1)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT status,failed_series FROM market_collection_runs"
        ).fetchone(), ("failed", 1))
        conn.close()

    def test_reconcile_rechecks_and_records_source_gap(self):
        start = 1_800_000_000_000
        end = start + 2 * BAR_MS["1m"]
        with patch.object(collect, "_symbols",
                          return_value=["NVDA-USDT-SWAP"]), \
                patch.object(collect, "_window", return_value=(start, end)), \
                patch.object(collect, "fetch_history_window", side_effect=[
                    ([_row(start)], 1), ([], 1)]):
            code = collect.main([
                "--reconcile-date", "2026-08-22", "--bar", "1m",
                "--inst", "NVDA-USDT-SWAP", "--db", self.db])
        self.assertEqual(code, 0)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT status FROM market_collection_runs").fetchone()[0],
            "success")
        self.assertEqual(conn.execute(
            "SELECT open_time,reason FROM market_data_gaps").fetchone(),
            (start + BAR_MS["1m"],
             "absent_from_okx_history_after_recheck"))
        conn.close()

    def test_backup_failure_returns_nonzero(self):
        with patch.object(upload.os.path, "exists", return_value=True), \
                patch.object(upload, "upload_file", return_value=False):
            self.assertEqual(upload.main(["--date", "2026-08-22"]), 1)


if __name__ == "__main__":
    unittest.main()
