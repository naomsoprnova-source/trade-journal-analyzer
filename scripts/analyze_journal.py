"""Analyze normalized, user-supplied closed-trade summaries; standard library only."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path


def number(value, name):
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing {name}")
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {name}") from exc
    if not n.is_finite():
        raise ValueError(f"nonfinite {name}")
    if len(n.as_tuple().digits) > 28 or not -28 <= n.as_tuple().exponent <= 28 or n.adjusted() > 28:
        raise ValueError(f"{name} exceeds supported precision: 28 significant digits, exponent -28..28, magnitude below 1e29")
    return n


def ratio(numerator, denominator):
    """Ratios are explicitly rounded to 28 significant digits, unlike monetary totals."""
    with localcontext() as context:
        context.prec = 28
        return numerator / denominator


def timestamp(value):
    if not value:
        raise ValueError("missing closed_at")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid closed_at") from exc
    if dt.tzinfo is None:
        raise ValueError("closed_at requires timezone")
    return dt.astimezone(timezone.utc)


def load_rows(path):
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        if path.suffix.lower() == ".json":
            data = json.load(stream, parse_float=Decimal)
            if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
                raise ValueError("JSON must be an array of trade objects")
            return data
        if path.suffix.lower() not in {".csv", ".tsv"}:
            raise ValueError("supported formats: CSV, TSV, JSON")
        reader = csv.DictReader(stream, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("missing or duplicate column headers")
        rows = list(reader)
        if any(None in r for r in rows):
            raise ValueError("row has extra columns")
        return rows


def gross_profit(row):
    if str(row.get("gross_pnl", "")).strip():
        return number(row["gross_pnl"], "gross_pnl")
    if row.get("instrument_type") != "linear":
        raise ValueError("missing gross_pnl; only explicit linear instruments can be derived")
    side = str(row.get("side", "")).lower()
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    qty = number(row.get("quantity"), "quantity")
    entry = number(row.get("entry_price"), "entry_price")
    exit_price = number(row.get("exit_price"), "exit_price")
    multiplier = number(row.get("contract_multiplier"), "contract_multiplier")
    if min(qty, entry, exit_price, multiplier) <= 0:
        raise ValueError("quantity, prices and multiplier must be positive")
    return (exit_price - entry) * qty * multiplier * (1 if side == "long" else -1)


def analyze(rows, initial_equity=None):
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("expected list of trade objects")
    # Bound each numeric input, then allow enough precision for four-factor products,
    # extreme scale differences and all row sums without monetary rounding.
    with localcontext() as context:
        context.prec = 256 + len(str(max(1, len(rows))))
        return _analyze(rows, initial_equity)


def _analyze(rows, initial_equity):
    equity = None if initial_equity is None else number(initial_equity, "initial_equity")
    if equity is not None and equity <= 0:
        raise ValueError("initial_equity must be positive")
    issues, buckets = [], {}
    counts = {}
    for row in rows:
        ident = str(row.get("trade_id") or "").strip()
        counts[ident] = counts.get(ident, 0) + 1
    unknown_currency = False
    open_rows = 0
    for index, row in enumerate(rows, 1):
        ident = str(row.get("trade_id") or "").strip()
        currency = str(row.get("currency") or "").strip().upper()
        errors = []
        if not currency:
            issues.append({"row": index, "errors": ["missing currency"]})
            unknown_currency = True
            continue
        bucket = buckets.setdefault(currency, {"records": [], "errors": [], "open_rows": 0})
        if not ident:
            errors.append("missing trade_id")
        elif counts[ident] > 1:
            errors.append("duplicate trade_id; do not silently deduplicate")
        status = str(row.get("status", "")).lower().strip()
        if status == "open" and not errors:
            open_rows += 1
            bucket["open_rows"] += 1
            continue
        if status != "closed":
            errors.append("status must explicitly be closed or open")
        record = {"row": index, "trade_id": ident}
        for field, calculate in (("gross", lambda: gross_profit(row)),
                                 ("fees", lambda: number(row.get("fees"), "fees")),
                                 ("funding", lambda: number(row.get("funding"), "funding"))):
            try:
                record[field] = calculate()
                if field == "fees" and record[field] < 0:
                    errors.append("fees must be nonnegative; rebates need explicit normalization")
            except ValueError as exc:
                errors.append(str(exc))
        try:
            record["time"] = timestamp(row.get("closed_at"))
        except ValueError as exc:
            record["time_error"] = str(exc)
        record["errors"] = errors
        if errors:
            issues.append({"row": index, "errors": errors})
            bucket["errors"].extend(errors)
        bucket["records"].append(record)
    if equity is not None and (len(buckets) != 1 or unknown_currency):
        raise ValueError("initial_equity requires one known currency")
    groups = {}
    for currency, bucket in sorted(buckets.items()):
        records = bucket["records"]
        complete = bool(records) and not bucket["errors"] and not unknown_currency
        result = {"closed_candidate_rows": len(records), "open_rows_excluded": bucket["open_rows"],
                  "net_status": "determined" if complete else "undetermined",
                  "gross_pnl": None, "fees": None, "funding": None, "net_pnl": None,
                  "wins": None, "losses": None, "breakeven": None,
                  "win_rate_pct": None, "profit_factor": None,
                  "max_drawdown_absolute": None, "max_drawdown_pct": None,
                  "drawdown_scope": "closed-trade realized curve, not intratrade or account equity",
                  "drawdown_status": "undetermined"}
        for source, target in (("gross", "gross_pnl"), ("fees", "fees"), ("funding", "funding")):
            if records and all(source in r for r in records) and not bucket["errors"] and not unknown_currency:
                result[target] = sum((r[source] for r in records), Decimal(0))
        if complete:
            for record in records:
                record["net"] = record["gross"] - record["fees"] + record["funding"]
            nets = [r["net"] for r in records]
            result["net_pnl"] = sum(nets, Decimal(0))
            result["wins"] = sum(n > 0 for n in nets)
            result["losses"] = sum(n < 0 for n in nets)
            result["breakeven"] = sum(n == 0 for n in nets)
            with localcontext() as context:
                context.prec = 28
                result["win_rate_pct"] = ratio(Decimal(result["wins"]), len(nets)) * 100
            gains = sum((n for n in nets if n > 0), Decimal(0))
            losses = -sum((n for n in nets if n < 0), Decimal(0))
            result["profit_factor"] = ratio(gains, losses) if losses else ("infinity_no_losses" if gains else None)
            if all("time" in r for r in records):
                # Treat simultaneous closes as one event; arbitrary row order must not create drawdown.
                events = {}
                for record in records:
                    events[record["time"]] = events.get(record["time"], Decimal(0)) + record["net"]
                curve = equity if equity is not None else Decimal(0)
                peak, max_dd, max_pct = curve, Decimal(0), Decimal(0)
                for time in sorted(events):
                    curve += events[time]
                    peak = max(peak, curve)
                    dd = peak - curve
                    max_dd = max(max_dd, dd)
                    if equity is not None:
                        with localcontext() as context:
                            context.prec = 28
                            max_pct = max(max_pct, ratio(dd, peak) * 100)
                result["max_drawdown_absolute"] = max_dd
                result["max_drawdown_pct"] = max_pct if equity is not None else None
                result["drawdown_status"] = "determined_absolute_and_pct" if equity is not None else "determined_absolute_only"
            else:
                result["drawdown_status"] = "undetermined_missing_or_invalid_close_times"
        groups[currency] = result
    return {"scope": "uploaded closed-trade summaries only; not verified account performance",
            "input_rows": len(rows), "open_rows_excluded": open_rows,
            "net_status": "determined" if groups and all(g["net_status"] == "determined" for g in groups.values()) and not unknown_currency else "undetermined",
            "groups": groups, "issues": issues,
            "notes": ["صافي الربح غير محسوم عند نقص البيانات الأساسية أو الرسوم أو التمويل أو معلومات الإغلاق.",
                      "Separate currencies are never summed; explicit zero fees/funding required.",
                      "No live accounts, network calls, orders, or profitability claims."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-equity", help="Confirmed starting equity in the sole currency; optional")
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve() or args.output.exists():
            raise ValueError("output must be a new file; never overwrite source or existing results")
        result = analyze(load_rows(args.input), args.initial_equity)
    except (ValueError, OSError, csv.Error) as exc:
        parser.exit(2, f"Input error: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"net_status={result['net_status']}; output={args.output}")


if __name__ == "__main__":
    main()
