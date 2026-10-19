"""Synthetic fixtures only: no actual user trades, network or performance claims."""
import csv
from decimal import Decimal as D
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from analyze_journal import analyze, load_rows


def sample():
    return [dict(trade_id=f"synthetic-{i}", status="closed", currency="USDT",
                 closed_at=f"2026-01-0{i}T10:00:00Z", gross_pnl=g, fees=f, funding=u)
            for i, g, f, u in [(1, "100", "2", "-3"), (2, "-40", "2", "1"), (3, "1", "1", "0")]]


class JournalTests(unittest.TestCase):
    def group(self, rows=None, equity=None):
        return analyze(sample() if rows is None else rows, equity)["groups"]["USDT"]

    def test_full_metrics(self):
        g = self.group(equity="1000")
        self.assertEqual([g[k] for k in ("gross_pnl", "fees", "funding", "net_pnl")], [D(61), D(5), D(-2), D(54)])
        self.assertEqual([g[k] for k in ("wins", "losses", "breakeven")], [1, 1, 1])
        self.assertEqual(g["win_rate_pct"], D(1)/3*100)
        self.assertEqual(g["profit_factor"], D(95)/41)
        self.assertEqual(g["max_drawdown_absolute"], D(41))
        self.assertEqual(g["max_drawdown_pct"], D(41)/1095*100)

    def test_missing_essential_fields(self):
        for field in ("gross_pnl", "fees", "funding", "status", "trade_id", "currency"):
            with self.subTest(field=field):
                rows = sample(); del rows[1][field]
                result = analyze(rows)
                self.assertEqual(result["net_status"], "undetermined")
                self.assertIsNone(result["groups"]["USDT"]["net_pnl"])

    def test_empty_fields_not_zero(self):
        for value in (None, "", " "):
            rows = sample(); rows[0]["fees"] = value
            self.assertIsNone(self.group(rows)["net_pnl"])

    def test_nonfinite_or_invalid_values(self):
        for value in ("NaN", "Infinity", "-Infinity", "bad", "1,000", "1e999999999999999999999"):
            with self.subTest(value=value):
                rows = sample(); rows[0]["gross_pnl"] = value
                self.assertEqual(analyze(rows)["net_status"], "undetermined")

    def test_negative_fee_rejected(self):
        rows = sample(); rows[0]["fees"] = "-1"
        self.assertIsNone(self.group(rows)["net_pnl"])

    def test_duplicate_rejected(self):
        rows = sample(); rows.append(dict(rows[0]))
        self.assertIsNone(self.group(rows)["net_pnl"])

    def test_open_excluded(self):
        rows = sample() + [dict(trade_id="synthetic-open", status="open", currency="USDT")]
        g = self.group(rows)
        self.assertEqual(g["net_pnl"], D(54))
        self.assertEqual(g["open_rows_excluded"], 1)

    def test_unknown_close_status(self):
        rows = sample(); rows[0]["status"] = "partially_closed"
        self.assertIsNone(self.group(rows)["net_pnl"])

    def test_no_close_time_no_drawdown(self):
        for value in (None, "bad", "2026-01-01T10:00:00"):
            rows = sample(); rows[0]["closed_at"] = value
            g = self.group(rows)
            self.assertEqual(g["net_pnl"], D(54))
            self.assertIsNone(g["max_drawdown_absolute"])

    def test_sorting_and_no_equity(self):
        rows = list(reversed(sample()))
        g = self.group(rows)
        self.assertEqual(g["max_drawdown_absolute"], D(41))
        self.assertIsNone(g["max_drawdown_pct"])

    def test_simultaneous_closes_grouped(self):
        rows = sample()
        for r in rows: r["closed_at"] = "2026-01-01T10:00:00Z"
        self.assertEqual(self.group(rows)["max_drawdown_absolute"], D(0))

    def test_loss_first_includes_start(self):
        rows = sample()[1:2]
        self.assertEqual(self.group(rows)["max_drawdown_absolute"], D(41))
        self.assertEqual(self.group(rows, "100")["max_drawdown_pct"], D(41))

    def test_all_wins_or_all_flat(self):
        rows = sample()[:1]
        self.assertEqual(self.group(rows)["profit_factor"], "infinity_no_losses")
        rows = sample()[2:]
        self.assertIsNone(self.group(rows)["profit_factor"])
        self.assertEqual(self.group(rows)["win_rate_pct"], D(0))

    def test_mixed_currency_no_sum(self):
        rows = sample(); rows[2]["currency"] = "USD"
        result = analyze(rows)
        self.assertEqual(set(result["groups"]), {"USD", "USDT"})
        self.assertNotIn("net_pnl", result)
        with self.assertRaises(ValueError): analyze(rows, "1000")

    def test_empty_and_open_only(self):
        self.assertEqual(analyze([])["net_status"], "undetermined")
        self.assertEqual(analyze([dict(trade_id="open", status="open", currency="USDT")])["net_status"], "undetermined")

    def test_linear_long_and_short(self):
        for side, expected in (("long", D(39)), ("short", D(-41))):
            rows = [dict(trade_id="linear", status="closed", currency="USDT", closed_at="2026-01-01T00:00:00Z",
                         instrument_type="linear", side=side, entry_price="100", exit_price="110",
                         quantity="2", contract_multiplier="2", fees="1", funding="0")]
            self.assertEqual(self.group(rows)["net_pnl"], expected)
            del rows[0]["contract_multiplier"]
            self.assertIsNone(self.group(rows)["net_pnl"])

    def test_inverse_not_assumed_linear(self):
        rows = sample(); del rows[0]["gross_pnl"]; rows[0]["instrument_type"] = "inverse"
        self.assertIsNone(self.group(rows)["net_pnl"])

    def test_invalid_initial_equity(self):
        for value in ("0", "-1", "NaN"):
            with self.assertRaises(ValueError): analyze(sample(), value)

    def test_decimal_accuracy(self):
        rows = sample()[:1]; rows[0].update(gross_pnl="0.3", fees="0.1", funding="0.0")
        self.assertEqual(self.group(rows)["net_pnl"], D("0.2"))

    def test_json_literal_preserves_decimal_digits(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"literal.json"
            content = '[{"trade_id":"synthetic-precise","status":"closed","currency":"USDT","gross_pnl":0.10000000000000001,"fees":0.1,"funding":0}]'
            source.write_text(content, encoding="utf-8")
            self.assertEqual(self.group(load_rows(source))["net_pnl"], D("0.00000000000000001"))
            self.assertEqual(self.group(load_rows(source))["wins"], 1)

    def test_excess_precision_rejected_not_rounded(self):
        for value in ("0.10000000000000000000000000009", "9999999999999999999999999999.9", "1e-29", "1e29"):
            rows = sample()[:1]; rows[0]["gross_pnl"] = value
            self.assertIsNone(self.group(rows)["net_pnl"])

    def test_wide_scale_exact_monetary_sum(self):
        rows = sample()[:1]
        rows[0].update(gross_pnl="1e28", fees="1e-28", funding="0")
        expected = D("9999999999999999999999999999.9999999999999999999999999999")
        self.assertEqual(self.group(rows)["net_pnl"], expected)

    def test_csv_tsv_json_and_unicode_paths(self):
        with tempfile.TemporaryDirectory(prefix="سجل اصطناعي ") as tmp:
            for ext in ("csv", "tsv", "json"):
                p = Path(tmp)/f"بيانات اختبار.{ext}"
                if ext == "json":
                    p.write_text(json.dumps(sample()), encoding="utf-8-sig")
                else:
                    with p.open("w", encoding="utf-8-sig", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=list(sample()[0]), delimiter="\t" if ext == "tsv" else ",")
                        w.writeheader(); w.writerows(sample())
                self.assertEqual(self.group(load_rows(p))["net_pnl"], D(54))

    def test_bad_file_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in (("bad.json", "{}"), ("bad.csv", "fees,fees\n1,2\n"),
                                  ("extra.csv", "fees\n1,2\n"), ("bad.txt", "hello")):
                p = Path(tmp)/name; p.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError): load_rows(p)

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix="اختبار CLI ") as tmp:
            source, output = Path(tmp)/"input.json", Path(tmp)/"output.json"
            source.write_text(json.dumps(sample()), encoding="utf-8")
            command = [sys.executable, str(Path(__file__).with_name("analyze_journal.py")), str(source), "--output", str(output), "--initial-equity", "1000"]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["groups"]["USDT"]["net_pnl"], "54")

    def test_core_without_network(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self.group()["net_pnl"], D(54))

    def test_duplicate_across_currencies_blocks_both(self):
        rows = sample()
        rows[1].update(trade_id=rows[0]["trade_id"], currency="USD")
        result = analyze(rows)
        self.assertTrue(all(g["net_status"] == "undetermined" for g in result["groups"].values()))

    def test_cli_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"input.json"
            original = json.dumps(sample())
            source.write_text(original, encoding="utf-8")
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("analyze_journal.py")),
                                     str(source), "--output", str(source)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
