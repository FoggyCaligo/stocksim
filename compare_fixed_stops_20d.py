from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd


BASE_RUNNER = Path(__file__).with_name("seed_sweep_envelope_cross_double_hold_target_range.py")
RESULT_ROOT = Path("results/compare_fixed_stops_20d")
STOP_VARIANTS = (("stop_3pct", 0.03), ("stop_4pct", 0.04))


def _strip_option(argv: list[str], option: str, takes_value: bool = True) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == option:
            i += 2 if takes_value and i + 1 < len(argv) else 1
            continue
        if takes_value and token.startswith(option + "="):
            i += 1
            continue
        out.append(token)
        i += 1
    return out


def _common_args(argv: list[str]) -> list[str]:
    # This experiment owns the exit settings so the two variants are directly comparable.
    out = list(argv)
    for option in (
        "--stop-gap-ratio",
        "--fixed-stop-loss-pct",
        "--max-hold-days",
        "--output-dir",
    ):
        out = _strip_option(out, option, takes_value=True)
    return out


def _run_variant(common: list[str], label: str, stop_loss: float) -> Path:
    out_dir = RESULT_ROOT / label
    cmd = [
        sys.executable,
        str(BASE_RUNNER),
        *common,
        "--max-hold-days",
        "20",
        "--fixed-stop-loss-pct",
        str(stop_loss),
        "--output-dir",
        str(out_dir),
    ]
    print("\n" + "=" * 72)
    print(f"Running {label}: max hold 20 trading days / fixed stop -{stop_loss * 100:g}%")
    print("=" * 72)
    subprocess.run(cmd, check=True)
    return out_dir


def _summary_row(label: str, stop_loss: float, summary: dict) -> dict:
    returns = summary.get("return_stats_pct", {})
    wins = summary.get("win_rate_stats_pct", {})
    counts = summary.get("mean_counts", {})
    diag = summary.get("trade_diagnostics", {})
    return {
        "variant": label,
        "fixed_stop_loss_pct": stop_loss * 100.0,
        "max_hold_days": 20,
        "mean_return_pct": returns.get("mean"),
        "median_return_pct": returns.get("median"),
        "min_return_pct": returns.get("min"),
        "max_return_pct": returns.get("max"),
        "positive_seed_rate_pct": returns.get("positive_seed_rate_pct"),
        "mean_win_rate_pct": wins.get("mean"),
        "mean_trades": counts.get("trades"),
        "mean_target_count": counts.get("target"),
        "mean_stop_count": counts.get("stop"),
        "mean_hold_exit_count": counts.get("hold_exit"),
        "avg_planned_target_return_pct": diag.get("average_planned_target_return_pct"),
        "avg_planned_stop_loss_pct": diag.get("average_planned_stop_loss_pct"),
        "target_hit_avg_holding_days": diag.get("target_hit_average_holding_days"),
        "hold_exit_avg_final_return_pct": diag.get("hold_exit_average_final_return_pct"),
    }


def main() -> None:
    common = _common_args(sys.argv[1:])
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    summaries: dict[str, dict] = {}
    for label, stop_loss in STOP_VARIANTS:
        out_dir = _run_variant(common, label, stop_loss)
        summary_path = out_dir / "seed_summary.json"
        with summary_path.open("r", encoding="utf-8") as fh:
            summary = json.load(fh)
        summaries[label] = summary
        rows.append(_summary_row(label, stop_loss, summary))

    comparison = pd.DataFrame(rows)
    comparison.to_csv(RESULT_ROOT / "comparison.csv", index=False, encoding="utf-8-sig")

    payload = {
        "experiment": {
            "base_runner": BASE_RUNNER.name,
            "max_hold_days": 20,
            "fixed_stop_variants_pct": [3.0, 4.0],
            "envelope_cross_basis": "low",
            "envelope_cross_lookback_days": 3,
            "hold_period_ratio": 2.0,
            "planned_target_return_range_pct": [8.0, 16.0],
            "note": "The underlying recent-touch-derived holding period is still scaled by 2x, then capped at 20 trading days.",
        },
        "variants": summaries,
    }
    with (RESULT_ROOT / "comparison.json").open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    print("\n=== Fixed-stop comparison ===")
    print(comparison.to_string(index=False))
    print(f"saved: {RESULT_ROOT / 'comparison.csv'}")
    print(f"saved: {RESULT_ROOT / 'comparison.json'}")


if __name__ == "__main__":
    main()
