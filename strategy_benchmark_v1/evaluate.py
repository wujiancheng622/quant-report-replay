"""Quantitative benchmark V1 (latest amended config) for an independent strategy account."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd

RULES = json.loads((Path(__file__).parent / "benchmark_v1.json").read_text(encoding="utf-8"))
ANCHORS = RULES["anchors"]


def years_between(a, b):
    return (b - a).total_seconds() / 86400 / 365.25


def read_submission(path):
    """Accept exactly one upstream-frozen official run, never a menu of scenarios."""
    path = Path(path)
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("Submission must be a single JSON object")
    if any(key in obj for key in ("variants", "runs", "scenarios", "candidates", "stress_runs", "alternative_runs")):
        raise ValueError("A submission must contain ONE official run, not multiple variants")
    required = ("strategy_id", "strategy_version", "strategy_type", "run_role",
                "cost_scenario", "ledger_path", "initial_capital", "cost_profile")
    for key in required:
        if key not in obj or obj[key] is None:
            raise ValueError(f"Missing submission field: {key}")
    if obj["run_role"] != "OFFICIAL_MAIN":
        raise ValueError("Only OFFICIAL_MAIN may be submitted to the benchmark Skill")
    if obj["cost_scenario"] != "NORMAL":
        raise ValueError("Only NORMAL cost scenario is accepted; HIGH and stress stay upstream")
    for key in ("strategy_id", "strategy_version", "strategy_type", "cost_profile"):
        if not isinstance(obj[key], str) or not obj[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if not isinstance(obj["ledger_path"], str) or not obj["ledger_path"].strip():
        raise ValueError("ledger_path must refer to exactly one file")
    ledger = Path(obj["ledger_path"]).expanduser()
    if not ledger.is_absolute():
        ledger = path.parent / ledger
    ledger = ledger.resolve()
    if not ledger.is_file():
        raise ValueError(f"Official ledger file not found: {ledger}")
    capital = obj["initial_capital"]
    if isinstance(capital, bool) or not isinstance(capital, (int, float)) or capital <= 0:
        raise ValueError("initial_capital must be a positive number")
    for key in ("date_column", "equity_column"):
        if key in obj and (not isinstance(obj[key], str) or not obj[key]):
            raise ValueError(f"{key} must be a non-empty string")
    if "proxy_used" in obj and not isinstance(obj["proxy_used"], bool):
        raise ValueError("proxy_used must be a boolean (passive metadata only)")
    return obj, ledger


def load_equity(path, *, date_col="date", nav_col="nav", index_date=False):
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    elif path.suffix.lower() == ".jsonl":
        frame = pd.read_json(path, lines=True)
    else:
        raise ValueError("Official ledger must be CSV, Parquet or JSONL")
    if nav_col not in frame.columns:
        raise ValueError(f"Equity column not found: {nav_col}")
    if not index_date and date_col not in frame.columns:
        raise ValueError(f"Date column not found: {date_col}")
    return frame.index if index_date else frame[date_col], frame[nav_col]


def assess(dates, nav, capital, initial_date=None):
    dates = pd.DatetimeIndex(pd.to_datetime(dates))
    nav = np.asarray(nav, dtype=float)
    if len(dates) < 2 or len(dates) != len(nav) or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError("Invalid or nonmonotonic/duplicate observations")
    if not np.isfinite(nav).all() or np.any(nav <= 0) or capital <= 0:
        raise ValueError("NAV and capital must be finite and positive")
    first = pd.Timestamp(initial_date) if initial_date else dates[0]
    if first > dates[0]:
        raise ValueError("Initial date after first observation")
    if first == dates[0] and abs(nav[0] - capital) < .001:
        d, v = dates, nav
    else:
        if first == dates[0]:
            first -= pd.Timedelta(days=1)
        d, v = dates.insert(0, first), np.r_[capital, nav]
    duration = years_between(d[0], d[-1])
    cagr = float((v[-1] / v[0]) ** (1 / duration) - 1)
    mdd = float(np.max(1 - v / np.maximum.accumulate(v)))
    # A mathematically exact 18%/25%/35% drawdown can be represented as
    # 0.18000000000000005 etc.; don't move it to the next risk bucket.
    mdd_gate = round(mdd, 12)
    daily = np.diff(v) / v[:-1]
    vol = float(np.std(daily, ddof=1) * np.sqrt(252))

    underwater = []
    peak_date, peak_val = d[0], v[0]
    in_drawdown = False
    for date, val in zip(d[1:], v[1:]):
        if val >= peak_val - max(1e-7, peak_val * 1e-12):
            if in_drawdown:
                underwater.append((peak_date, date, True))
                in_drawdown = False
            peak_date, peak_val = date, val
        else:
            in_drawdown = True
    if in_drawdown:
        underwater.append((peak_date, d[-1], False))
    longest = max(underwater, key=lambda x: (x[1] - x[0]).days) if underwater else None

    if mdd_gate <= ANCHORS[0]["drawdown"]:
        required, bucket = ANCHORS[0]["min_cagr"], ANCHORS[0]
    elif mdd_gate > ANCHORS[-1]["drawdown"]:
        required, bucket = None, ANCHORS[-1]
    else:
        for left, right in zip(ANCHORS[:-1], ANCHORS[1:]):
            if left["drawdown"] < mdd_gate <= right["drawdown"]:
                f = (mdd_gate - left["drawdown"]) / (right["drawdown"] - left["drawdown"])
                required = left["min_cagr"] + f * (right["min_cagr"] - left["min_cagr"])
                bucket = right
                break
    return_pass = required is not None and cagr + 1e-12 >= required

    roll = {}
    for term in (3, 5):
        records = []
        month_groups = pd.Series(np.arange(len(d)), index=d).groupby(d.to_period("M"))
        for _, group in month_groups:
            i = int(group.iloc[0])
            j = int(d.searchsorted(d[i] + pd.DateOffset(years=term)))
            if j < len(d):
                records.append(float((v[j] / v[i]) ** (1 / years_between(d[i], d[j])) - 1))
        roll[str(term)] = {"count": len(records),
                          "profit_ratio": sum(x > 0 for x in records) / len(records) if records else None,
                          "worst_cagr": min(records) if records else None,
                          "median_cagr": float(np.median(records)) if records else None}
    r3 = roll["3"]["profit_ratio"]
    stability = "insufficient" if r3 is None else ("较稳定" if r3 >= .7 else "一般" if r3 >= .5 else "较差")
    series = pd.Series(v, index=d)
    ends = series.groupby(series.index.year).last()
    annual, last = {}, v[0]
    for yr, value in ends.items():
        annual[str(yr)] = float(value / last - 1)
        last = value
    return {
        "start": str(d[0].date()), "end": str(d[-1].date()), "years": duration,
        "cagr": cagr, "mdd": mdd, "volatility": vol,
        "required_cagr": required, "bucket": bucket["label"],
        "recovery_is_gate": False,
        "longest_underwater": None if longest is None else {
            "peak": str(longest[0].date()), "end": str(longest[1].date()),
            "recovered": longest[2], "months": years_between(longest[0], longest[1]) * 12
        },
        "return_drawdown_pass": bool(return_pass),
        "numeric_v1_pass": bool(return_pass),
        "stability": stability, "rolling": roll, "annual": annual
    }


def main():
    p = argparse.ArgumentParser()
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--submission", type=Path, help="One upstream-frozen official main run manifest (Skill entry).")
    source.add_argument("--input", type=Path, help="Direct numeric diagnostic only, not the Skill workflow.")
    p.add_argument("--name")
    p.add_argument("--date-col", default="date")
    p.add_argument("--nav-col", default="nav")
    p.add_argument("--index-date", action="store_true")
    p.add_argument("--capital", type=float)
    p.add_argument("--initial-date")
    p.add_argument("--proxy-used", action="store_true", help="Optional passive provenance label; never affects V1 decision.")
    p.add_argument("--proxy-note", default="", help="Optional upstream-supplied provenance note; not assessed here.")
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    if a.submission is not None:
        if a.capital is not None or a.name is not None or a.initial_date is not None or a.proxy_used or a.proxy_note:
            p.error("When --submission is used, all run-specific values must come from the official manifest")
        info, path = read_submission(a.submission)
        dates, nav = load_equity(
            path,
            date_col=info.get("date_column", "date"),
            nav_col=info.get("equity_column", "nav"),
            index_date=info.get("index_date", False),
        )
        capital, name = info["initial_capital"], info["strategy_id"]
        first = info.get("initial_date")
        proxy_used, proxy_note = info.get("proxy_used", False), info.get("proxy_note", "")
        metadata = {
            "submission_path": str(a.submission.resolve()),
            "run_role": "OFFICIAL_MAIN",
            "cost_scenario": "NORMAL",
            "cost_profile": info["cost_profile"],
            "strategy_type": info["strategy_type"],
            "strategy_version": info["strategy_version"],
            "source_run_id": info.get("source_run_id"),
        }
    else:
        if a.capital is None or a.name is None:
            p.error("Direct diagnostic requires --name and --capital")
        path = a.input
        dates, nav = load_equity(path, date_col=a.date_col, nav_col=a.nav_col, index_date=a.index_date)
        capital, name, first = a.capital, a.name, a.initial_date
        proxy_used, proxy_note = a.proxy_used, a.proxy_note
        metadata = {"run_role": "DIAGNOSTIC_ONLY"}
    metrics = assess(dates, nav, capital, first)
    result = {"name": name, "source": str(path), "benchmark": RULES["version"],
              "benchmark_revision": RULES.get("revision", "V1-initial"),
              "proxy_used": bool(proxy_used),
              "proxy_note": proxy_note,
              "screening_status": "PASS" if metrics["numeric_v1_pass"] else "FAIL",
              "next_step": RULES["post_pass_review"] if metrics["numeric_v1_pass"] else "STOP_SCREENING",
              **metadata,
              **metrics}
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
