"""Compute per-trial walking speed and per-axis sway metrics for the DTG
outbound/return walking segments, excluding the turn itself.

Segmentation, per trial:
  - start/stop events use the human-reviewed method choice
    (review/onset_review_labels.csv: 'thresh' or 'turn' per event), not a
    single blanket rule, since the review found this genuinely varies by
    trial (52% pattern: threshold for start, turn-anchored for stop; see
    README.md).
  - the turn location is the manually-corrected one where the reviewer
    fixed it (manual_turn_s), else the auto-detected one (turn_time_s).
  - outbound  = [start_event, turn - TURN_MARGIN_S]
  - turn      = [turn - TURN_MARGIN_S, turn + TURN_MARGIN_S]   (excluded)
  - return    = [turn + TURN_MARGIN_S, stop_event]

Trials marked 'unclear' for an event, or with a degenerate (near-zero or
negative) outbound/return segment after excluding the turn margin, are
skipped and reported separately rather than silently producing bad numbers.

Axis convention (per study lead): Acc_X = vertical, Acc_Y = lateral (M/L),
Acc_Z = forward/backward (AP). Confirmed against quiet-stance segments —
Y sits near zero (no lateral tilt at rest) while gravity splits X/Z,
consistent with a belt mounted with some forward/backward tilt rather than
perfectly level (X still carries the majority of gravity).

Walking speed uses the protocol's known 3 m distance divided by segment
duration, rather than integrating acceleration (which drifts) — see
README.md for why double integration was ruled out.

Outputs: output/gait_metrics.csv (one row per trial), and a short
console summary.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent.parent
TURN_MARGIN_S = 2.0
WALK_DISTANCE_M = 3.0
IGNORE_TRIALS = {"SWEAT_005DTG2", "SWEAT_035DTG1"}  # confirmed unusable/unclear after review

AXES = {"Acc_X": "vertical", "Acc_Y": "lateral", "Acc_Z": "ap"}


def axis_stats(seg: pd.DataFrame, prefix: str) -> dict:
    out = {}
    for col, name in AXES.items():
        v = seg[col].to_numpy()
        out[f"{prefix}_{name}_mean"] = float(np.mean(v))
        out[f"{prefix}_{name}_sd"] = float(np.std(v, ddof=0))
        out[f"{prefix}_{name}_range"] = float(np.ptp(v))
    return out


def main():
    summary = pd.read_csv(HERE / "output" / "trial_summary.csv")
    labels = pd.read_csv(HERE / "review" / "onset_review_labels.csv")
    samples = pd.read_parquet(HERE / "output" / "imu_samples_tagged.parquet",
                               columns=["sheet", "t_s", "Acc_X", "Acc_Y", "Acc_Z"])

    labels = labels.set_index("sheet")
    summary = summary.set_index("sheet")

    rows = []
    skipped = []

    for sheet in summary.index:
        if sheet in IGNORE_TRIALS:
            skipped.append((sheet, "excluded (unusable/unclear per review)"))
            continue
        if sheet not in labels.index:
            skipped.append((sheet, "no review label"))
            continue

        lab = labels.loc[sheet]
        s = summary.loc[sheet]
        start_choice, stop_choice = lab["start_choice"], lab["stop_choice"]
        if start_choice == "unclear" or stop_choice == "unclear" or pd.isna(start_choice) or pd.isna(stop_choice):
            skipped.append((sheet, "unclear/missing review choice"))
            continue

        duration_s = s["n_samples"] / s["fs_hz"]
        logged = s["logged_duration_s"]
        turn = lab["manual_turn_s"] if pd.notna(lab["manual_turn_s"]) else s["turn_time_s"]

        if pd.notna(logged) and pd.notna(turn):
            onset_turn = max(0.0, turn - logged / 2)
            offset_turn = min(duration_s, turn + logged / 2)
        else:
            onset_turn, offset_turn = s["onset_s"], s["offset_s"]

        start_time = s["bout_onset_s"] if start_choice == "thresh" else onset_turn
        stop_time = s["bout_offset_s"] if stop_choice == "thresh" else offset_turn

        turn_lo, turn_hi = turn - TURN_MARGIN_S, turn + TURN_MARGIN_S
        outbound_end = min(turn_lo, stop_time)
        return_start = max(turn_hi, start_time)

        if outbound_end - start_time < 1.0:
            skipped.append((sheet, f"outbound segment too short ({outbound_end - start_time:.2f}s)"))
            continue
        if stop_time - return_start < 1.0:
            skipped.append((sheet, f"return segment too short ({stop_time - return_start:.2f}s)"))
            continue

        trial_samples = samples[samples.sheet == sheet]
        outbound = trial_samples[(trial_samples.t_s >= start_time) & (trial_samples.t_s <= outbound_end)]
        ret = trial_samples[(trial_samples.t_s >= return_start) & (trial_samples.t_s <= stop_time)]

        row = {
            "sheet": sheet,
            "participant": s["participant"],
            "condition": s["condition"],
            "start_choice": start_choice,
            "stop_choice": stop_choice,
            "turn_s": turn,
            "start_time_s": start_time,
            "outbound_end_s": outbound_end,
            "return_start_s": return_start,
            "stop_time_s": stop_time,
            "outbound_duration_s": outbound_end - start_time,
            "return_duration_s": stop_time - return_start,
            "outbound_speed_mps": WALK_DISTANCE_M / (outbound_end - start_time),
            "return_speed_mps": WALK_DISTANCE_M / (stop_time - return_start),
        }
        row.update(axis_stats(outbound, "outbound"))
        row.update(axis_stats(ret, "return"))
        rows.append(row)

    out = pd.DataFrame(rows)
    out_path = HERE / "output" / "gait_metrics.csv"
    out.to_csv(out_path, index=False)

    print(f"Computed metrics for {len(out)}/{len(summary)} trials")
    print(f"Skipped {len(skipped)}:")
    for sheet, reason in skipped:
        print(f"  {sheet}: {reason}")
    print()
    print("Walking speed (m/s) summary:")
    print(out[["outbound_speed_mps", "return_speed_mps"]].describe())
    print()
    print("Per-axis SD (sway), outbound segment:")
    print(out[[c for c in out.columns if c.startswith("outbound_") and c.endswith("_sd")]].describe())
    print()
    print("Wrote", out_path)


if __name__ == "__main__":
    main()
