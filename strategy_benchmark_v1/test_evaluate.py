"""Focused regression checks for the revised V1 numeric screening contract."""
import json
import sys

import pandas as pd
import pytest

from evaluate import ANCHORS, RULES, assess, main, read_submission


@pytest.mark.parametrize("drawdown,expected_bucket", [
    (0.08, "低风险"), (0.18, "稳健"), (0.25, "均衡"), (0.35, "进取"),
])
def test_recovery_no_longer_affects_any_risk_bucket(drawdown, expected_bucket):
    nav = [100, 200, 200 * (1 - drawdown), 350]
    dates = ["2020-01-01", "2020-02-01", "2020-03-01", "2025-03-01"]
    out = assess(dates, nav, 100)
    assert out["bucket"] == expected_bucket
    assert out["longest_underwater"]["months"] > 60
    assert out["recovery_is_gate"] is False
    assert "recovery_pass" not in out
    assert "recovery_limit_months" not in out
    assert out["numeric_v1_pass"]


def test_long_underwater_duration_still_reported_but_does_not_fail():
    dates = ["2020-01-01", "2020-02-01", "2020-03-01", "2025-03-01"]
    out = assess(dates, [100, 200, 170, 202], 100)
    assert out["bucket"] == "稳健"
    assert out["longest_underwater"]["months"] > 60
    assert out["longest_underwater"]["recovered"] is True
    assert out["numeric_v1_pass"] is True


def test_open_drawdown_is_not_mistaken_for_recovery_or_failed():
    dates = ["2020-01-01", "2020-02-01", "2020-03-01", "2025-03-01"]
    out = assess(dates, [100, 200, 175, 190], 100)
    assert out["longest_underwater"]["recovered"] is False
    assert out["longest_underwater"]["months"] > 60
    assert out["numeric_v1_pass"] is True


def test_above_35_percent_drawdown_still_fails():
    dates = ["2020-01-01", "2020-02-01", "2020-03-01", "2023-01-01"]
    out = assess(dates, [100, 200, 128, 250], 100)
    assert out["mdd"] > 0.35
    assert out["required_cagr"] is None
    assert not out["numeric_v1_pass"]


def test_proxy_flag_is_only_passive_metadata(tmp_path, monkeypatch, capsys):
    csv = tmp_path / "equity.csv"
    pd.DataFrame({
        "date": ["2020-01-01", "2021-01-01", "2022-01-01", "2023-01-01", "2024-01-01"],
        "nav": [100, 115, 130, 145, 170],
    }).to_csv(csv, index=False)
    monkeypatch.setattr(sys, "argv", [
        "evaluate.py", "--input", str(csv), "--name", "proxy_test",
        "--capital", "100", "--proxy-used", "--proxy-note", "指数替代",
    ])
    main()
    d = json.loads(capsys.readouterr().out)
    assert d["benchmark_revision"] == RULES["revision"]
    assert d["numeric_v1_pass"]
    assert d["proxy_used"]
    assert "proxy_followup_required" not in d
    assert d["screening_status"] == "PASS"
    assert d["next_step"] == "HUMAN_REVIEW"


def test_proxy_and_nonproxy_have_identical_decisions(tmp_path, monkeypatch, capsys):
    csv = tmp_path / "equity.csv"
    pd.DataFrame({
        "date": ["2020-01-01", "2021-01-01", "2022-01-01", "2023-01-01", "2024-01-01"],
        "nav": [100, 115, 130, 145, 170],
    }).to_csv(csv, index=False)
    results = []
    for extra in ([], ["--proxy-used", "--proxy-note", "经上游验收"]):
        monkeypatch.setattr(sys, "argv", [
            "evaluate.py", "--input", str(csv), "--name", "same_strategy",
            "--capital", "100", *extra,
        ])
        main()
        results.append(json.loads(capsys.readouterr().out))
    assert results[0]["screening_status"] == results[1]["screening_status"] == "PASS"
    assert results[0]["numeric_v1_pass"] == results[1]["numeric_v1_pass"]
    assert results[0]["required_cagr"] == results[1]["required_cagr"]


def test_full_four_anchor_contract():
    assert [e["min_cagr"] for e in ANCHORS] == [0.06, 0.10, 0.15, 0.20]
    assert [e["drawdown"] for e in ANCHORS] == [0.10, 0.18, 0.25, 0.35]
    assert all("recovery_months" not in e for e in ANCHORS)
    assert RULES["post_pass_review"] == "HUMAN_REVIEW"


def official_fixture(tmp_path):
    source = tmp_path / "one_main.csv"
    pd.DataFrame({
        "date": ["2020-01-01", "2021-01-01", "2022-01-01", "2023-01-01", "2024-01-01"],
        "equity": [100, 115, 130, 145, 170],
    }).to_csv(source, index=False)
    return {
        "strategy_id": "one_candidate",
        "strategy_version": "frozen_v1",
        "strategy_type": "equity_etf_rotation",
        "run_role": "OFFICIAL_MAIN",
        "cost_scenario": "NORMAL",
        "cost_profile": "upstream_approved_fee_slippage",
        "ledger_path": str(source),
        "initial_capital": 100,
        "date_column": "date",
        "equity_column": "equity",
    }


def test_official_submission_one_normal_run(tmp_path, monkeypatch, capsys):
    submission = tmp_path / "official.json"
    submission.write_text(json.dumps(official_fixture(tmp_path)), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "evaluate.py", "--submission", str(submission),
    ])
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["run_role"] == "OFFICIAL_MAIN"
    assert out["cost_scenario"] == "NORMAL"
    assert out["strategy_version"] == "frozen_v1"
    assert out["screening_status"] == "PASS"
    assert out["next_step"] == "HUMAN_REVIEW"
    assert out["benchmark_revision"] == RULES["revision"]


@pytest.mark.parametrize("changes", [
    {"cost_scenario": "HIGH"},
    {"cost_scenario": "BASE"},
    {"run_role": "STRESS"},
    {"variants": ["BASE", "HIGH"]},
    {"runs": [{"cost_scenario": "NORMAL"}]},
    {"ledger_path": ["low.csv", "high.csv"]},
])
def test_official_submission_rejects_multi_version_or_stress(tmp_path, changes):
    manifest = official_fixture(tmp_path)
    manifest.update(changes)
    location = tmp_path / "official.json"
    location.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        read_submission(location)
