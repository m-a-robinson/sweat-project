"""Ingest the SWEAT-DTG pelvis IMU export into a tidy, tagged dataset and
detect the quiet-stance periods at the start/end of each trial.

Each of the 149 sheets in ``SWEAT_DTG IMU Summary Data_ALL.xlsx`` holds one
participant x condition trial (e.g. ``SWEAT_001DTG1``): a block of ``//``
metadata lines followed by a header row (``PacketCounter, [SampleTimeFine,]
Acc_X, Acc_Y, Acc_Z, Roll, Pitch, Yaw``) and ~100 Hz samples.

There is no independent marker for gait start/end in the recording, only the
participant standing still for calibration before and (sometimes) after the
walk. This script finds that quiet stance automatically: it computes a
rolling standard deviation of the acceleration-vector magnitude and looks for
where it steps up from (roughly) sensor-noise level to walking level, and
back down again — a mean + k*SD threshold ("2SD change") measured from a
baseline window, per the study notes.

Outputs (under --outdir, default "output/"):
  - imu_samples_tagged.parquet   long-format, every sample from every trial,
                                  tagged with participant/condition and a
                                  phase label (pre_quiet / active / post_quiet
                                  / untagged_tail).
  - trial_summary.csv            one row per trial: detected onset/offset,
                                  detected duration, logged duration (joined
                                  from the Trial Times spreadsheet), and QC
                                  flags.
  - diagnostic_plots/*.png       a handful of example trials with the
                                  detection overlaid, for visual sanity-check.

Usage:
    python scripts/ingest_imu.py
    python scripts/ingest_imu.py --k 2.5 --plot-n 12
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

FS_DEFAULT = 100.0  # Hz. Confirmed from continuous PacketCounter increments
                     # against known trial durations (Trial Times spreadsheet).

SHEET_RE = re.compile(r"^SWEAT_(\d{3})\s*(DTG\s*TRIAL|DTG[123])$", re.IGNORECASE)


def parse_sheet_name(sheet_name: str) -> tuple[str, str]:
    """'SWEAT_001DTG1' -> ('SWEAT-001', 'DTG1'); 'SWEAT_005DTG TRIAL' -> ('SWEAT-005', 'DTG_TRIAL')."""
    m = SHEET_RE.match(sheet_name.strip())
    if not m:
        raise ValueError(f"Unrecognized sheet name format: {sheet_name!r}")
    pid, cond = m.groups()
    cond = re.sub(r"\s+", "", cond).upper()  # 'DTG TRIAL' -> 'DTGTRIAL'
    if cond == "DTGTRIAL":
        cond = "DTG_TRIAL"
    return f"SWEAT-{pid}", cond


def load_trial_raw(ws) -> pd.DataFrame:
    """Read one worksheet, skip the '//' metadata block, return raw columns."""
    rows = list(ws.iter_rows(values_only=True))
    hdr_idx = next(i for i, r in enumerate(rows) if r and r[0] == "PacketCounter")
    header = [h for h in rows[hdr_idx] if h is not None]
    data = rows[hdr_idx + 1:]
    df = pd.DataFrame(data, columns=list(rows[hdr_idx][: len(header)]))
    df = df[header]  # drop any trailing all-None columns
    if "SampleTimeFine" in df.columns:
        df = df.drop(columns=["SampleTimeFine"])  # unused/empty in this export
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(how="all").reset_index(drop=True)
    return df


def add_derived_signals(df: pd.DataFrame, fs: float) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    df["t_s"] = np.arange(n) / fs
    df["acc_mag"] = np.sqrt(df["Acc_X"] ** 2 + df["Acc_Y"] ** 2 + df["Acc_Z"] ** 2)

    # Pseudo-angular-rate from the orientation channels (no raw gyro is
    # exported). Unwrap first so genuine continuous rotation isn't mistaken
    # for a jump at the +-180 deg boundary. NOTE: Roll/Pitch/Yaw are Euler
    # angles and can still swing rapidly near gimbal-lock (pitch ~ +-90 deg,
    # observed around the mid-trial turn) — treat this signal as informative
    # for the gait/turn portion, not a robust quiet-detector by itself, hence
    # detect_quiet() below is driven by acc_mag rather than this.
    roll_u = np.degrees(np.unwrap(np.radians(df["Roll"].to_numpy())))
    pitch_u = np.degrees(np.unwrap(np.radians(df["Pitch"].to_numpy())))
    yaw_u = np.degrees(np.unwrap(np.radians(df["Yaw"].to_numpy())))
    t = df["t_s"].to_numpy()
    df["roll_rate"] = np.gradient(roll_u, t) if n > 1 else 0.0
    df["pitch_rate"] = np.gradient(pitch_u, t) if n > 1 else 0.0
    df["yaw_rate"] = np.gradient(yaw_u, t) if n > 1 else 0.0
    df["gyro_mag"] = np.sqrt(df["roll_rate"] ** 2 + df["pitch_rate"] ** 2 + df["yaw_rate"] ** 2)
    return df


def rolling_std(x: np.ndarray, win: int) -> np.ndarray:
    win = max(win, 1)
    s = pd.Series(x).rolling(window=win, center=True, min_periods=1).std(ddof=0)
    return s.to_numpy()


@dataclass
class Bout:
    start_idx: int
    end_idx: int  # inclusive
    duration_s: float


@dataclass
class QuietDetectionResult:
    onset_idx: int
    offset_idx: int
    start_detected: bool
    end_detected: bool
    offset_source: str  # 'detected' | 'end_of_recording'
    baseline_mean: float
    baseline_sd: float
    threshold: float
    n_bouts: int
    other_bouts: list  # Bout objects for any extra movement bouts, e.g. repeated reps


def _global_quiet_baseline(acc_sd: np.ndarray, low_percentile: float = 20.0) -> tuple[float, float]:
    """Estimate the sensor's stationary noise floor for this trial from the
    lower tail of the rolling-SD distribution across the WHOLE recording,
    rather than a single scanned window — a single window's own internal
    variance is a fragile (noisy) sd estimate and can blow the threshold up
    if that window happens to catch one small transient. Quiet stance
    (start, end, or an idle stretch mid-recording) reliably makes up the
    bottom of the distribution in every trial inspected."""
    cutoff = np.percentile(acc_sd, low_percentile)
    low_vals = acc_sd[acc_sd <= cutoff]
    return float(low_vals.mean()), float(low_vals.std())


def _find_bouts(active: np.ndarray, merge_gap_n: int, min_bout_n: int) -> list[Bout]:
    """Turn a boolean 'is this sample active' array into a list of bouts:
    bridge short quiet gaps inside a bout (merge_gap_n), then drop bouts
    shorter than min_bout_n (noise blips / knocks, not real gait)."""
    n = len(active)
    idx = np.flatnonzero(active)
    if len(idx) == 0:
        return []
    # merge runs separated by small gaps
    gaps = np.flatnonzero(np.diff(idx) > merge_gap_n)
    starts = np.concatenate(([idx[0]], idx[gaps + 1]))
    ends = np.concatenate((idx[gaps], [idx[-1]]))
    bouts = [Bout(int(s), int(e), 0.0) for s, e in zip(starts, ends) if (e - s + 1) >= min_bout_n]
    return bouts


def detect_quiet_periods(
    df: pd.DataFrame,
    fs: float,
    k: float = 2.0,
    roll_window_s: float = 0.25,
    merge_gap_s: float = 0.5,
    min_bout_s: float = 1.0,
    logged_duration_s: float | None = None,
) -> QuietDetectionResult:
    """Segment the trial into quiet/active bouts from Acc magnitude, using a
    single threshold = baseline_mean + k*baseline_sd (the "2SD" rule) taken
    from the lower tail of the rolling-SD distribution across the whole
    recording — not just the first/last couple of seconds, since some sheets
    run on well past the end of the timed trial (idle IMU) with occasional
    movement blips in that tail that a start/end-only search mistakes for
    the real trial boundary.

    If more than one bout survives (e.g. a repeated attempt, per the study
    notes' "repeated rep" case), the bout whose duration is closest to the
    logged trial time is picked as the primary trial; the rest are kept as
    `other_bouts` for QC rather than silently discarded.
    """
    n = len(df)
    roll_n = max(int(round(roll_window_s * fs)), 1)
    merge_gap_n = max(int(round(merge_gap_s * fs)), 1)
    min_bout_n = max(int(round(min_bout_s * fs)), 1)

    acc_sd = rolling_std(df["acc_mag"].to_numpy(), roll_n)
    baseline_mean, baseline_sd = _global_quiet_baseline(acc_sd)
    threshold = baseline_mean + k * baseline_sd

    bouts = _find_bouts(acc_sd > threshold, merge_gap_n, min_bout_n)
    for b in bouts:
        b.duration_s = (b.end_idx - b.start_idx + 1) / fs

    if not bouts:
        # no activity distinguishable from noise floor at all
        return QuietDetectionResult(0, n - 1, False, False, "end_of_recording",
                                     baseline_mean, baseline_sd, threshold, 0, [])

    if logged_duration_s is not None and not np.isnan(logged_duration_s) and len(bouts) > 1:
        primary = min(bouts, key=lambda b: abs(b.duration_s - logged_duration_s))
    else:
        primary = max(bouts, key=lambda b: b.duration_s)
    other = [b for b in bouts if b is not primary]

    start_detected = primary.start_idx > 0
    end_detected = primary.end_idx < n - 1
    offset_source = "detected" if end_detected else "end_of_recording"

    return QuietDetectionResult(
        onset_idx=primary.start_idx,
        offset_idx=primary.end_idx,
        start_detected=start_detected,
        end_detected=end_detected,
        offset_source=offset_source,
        baseline_mean=baseline_mean,
        baseline_sd=baseline_sd,
        threshold=threshold,
        n_bouts=len(bouts),
        other_bouts=other,
    )


def tag_phases(df: pd.DataFrame, result: QuietDetectionResult) -> pd.Series:
    n = len(df)
    phase = np.full(n, "active", dtype=object)
    phase[: result.onset_idx] = "pre_quiet"
    if result.offset_source == "detected":
        phase[result.offset_idx + 1:] = "post_quiet"
    for b in result.other_bouts:
        phase[b.start_idx: b.end_idx + 1] = "active_other"
    return pd.Series(phase, index=df.index, name="phase")


def load_trial_times(path: Path) -> pd.DataFrame:
    """Melt the Trial Times spreadsheet to (participant, condition, logged_duration_s)."""
    raw = pd.read_excel(path, sheet_name="DTG Trial Times", header=4)
    raw = raw.rename(columns={raw.columns[0]: "Date", raw.columns[1]: "Participant",
                               raw.columns[2]: "DTG_TRIAL", raw.columns[3]: "DTG1",
                               raw.columns[4]: "DTG2", raw.columns[5]: "DTG3",
                               raw.columns[6]: "Notes"})
    raw = raw[raw["Participant"].astype(str).str.startswith("SWEAT")]
    long = raw.melt(id_vars=["Participant", "Notes"], value_vars=["DTG_TRIAL", "DTG1", "DTG2", "DTG3"],
                     var_name="condition", value_name="logged_duration_s")
    long = long.rename(columns={"Participant": "participant"})
    return long[["participant", "condition", "logged_duration_s", "Notes"]]


def process_all(source_xlsx: Path, trial_times_xlsx: Path, outdir: Path, k: float,
                 fs: float, plot_n: int) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    plot_dir = outdir / "diagnostic_plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.load_workbook(source_xlsx, read_only=True, data_only=True)
    trial_times = load_trial_times(trial_times_xlsx)

    tagged_frames = []
    summary_rows = []
    plotted = 0

    for sheet_name in wb.sheetnames:
        try:
            participant, condition = parse_sheet_name(sheet_name)
        except ValueError as e:
            print(f"skip {sheet_name!r}: {e}")
            continue

        raw = load_trial_raw(wb[sheet_name])
        if raw.empty:
            print(f"skip {sheet_name!r}: no data rows")
            continue

        df = add_derived_signals(raw, fs)

        logged = trial_times[(trial_times.participant == participant) & (trial_times.condition == condition)]
        logged_duration_s = float(logged["logged_duration_s"].iloc[0]) if len(logged) and pd.notna(logged["logged_duration_s"].iloc[0]) else np.nan
        notes = logged["Notes"].iloc[0] if len(logged) else None

        result = detect_quiet_periods(df, fs, k=k, logged_duration_s=logged_duration_s)
        df["phase"] = tag_phases(df, result)
        df.insert(0, "condition", condition)
        df.insert(0, "participant", participant)
        df.insert(0, "sheet", sheet_name)
        tagged_frames.append(df)

        detected_duration_s = (result.offset_idx - result.onset_idx) / fs
        summary_rows.append({
            "sheet": sheet_name,
            "participant": participant,
            "condition": condition,
            "n_samples": len(df),
            "fs_hz": fs,
            "onset_s": result.onset_idx / fs,
            "offset_s": result.offset_idx / fs,
            "detected_duration_s": detected_duration_s,
            "logged_duration_s": logged_duration_s,
            "duration_diff_s": detected_duration_s - logged_duration_s if pd.notna(logged_duration_s) else np.nan,
            "start_detected": result.start_detected,
            "end_detected": result.end_detected,
            "offset_source": result.offset_source,
            "n_bouts": result.n_bouts,
            "n_other_bouts": len(result.other_bouts),
            "baseline_mean": result.baseline_mean,
            "baseline_sd": result.baseline_sd,
            "threshold": result.threshold,
            "trial_notes": notes,
        })

        if plot_n and plotted < plot_n:
            plot_trial(df, result, sheet_name, fs, plot_dir)
            plotted += 1

        flag = f" [+{len(result.other_bouts)} other bout(s)]" if result.other_bouts else ""
        print(f"{sheet_name:22s} n={len(df):5d} onset={result.onset_idx/fs:5.2f}s "
              f"offset={result.offset_idx/fs:5.2f}s ({result.offset_source}) "
              f"detected_dur={detected_duration_s:5.2f}s logged={logged_duration_s}{flag}")

    tagged = pd.concat(tagged_frames, ignore_index=True)
    tagged.to_parquet(outdir / "imu_samples_tagged.parquet", index=False)

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(outdir / "trial_summary.csv", index=False)

    n_end_detected = summary["end_detected"].sum()
    n_multi_bout = (summary["n_bouts"] > 1).sum()
    print()
    print(f"Wrote {len(tagged):,} tagged samples across {len(summary)} trials to {outdir}")
    print(f"Start quiet period detected in {summary['start_detected'].sum()}/{len(summary)} trials")
    print(f"End quiet period detected in {n_end_detected}/{len(summary)} trials "
          f"({100*n_end_detected/len(summary):.0f}%) — the rest run to end-of-recording "
          f"while still active (offset_source='end_of_recording' in trial_summary.csv)")
    print(f"{n_multi_bout} trials had more than one movement bout (repeated attempt / extra "
          f"movement in the recording); the bout closest to the logged trial duration was "
          f"picked as the primary trial, others tagged phase='active_other'")


def plot_trial(df: pd.DataFrame, result: QuietDetectionResult, sheet_name: str, fs: float, plot_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
    t = df["t_s"].to_numpy()
    axes[0].plot(t, df["acc_mag"])
    axes[0].set_ylabel("|Acc| (m/s^2)")
    axes[0].set_title(sheet_name)

    roll_n = max(int(round(0.25 * fs)), 1)
    axes[1].plot(t, rolling_std(df["acc_mag"].to_numpy(), roll_n))
    axes[1].set_ylabel("rolling SD |Acc|")
    axes[1].set_xlabel("time (s)")

    for ax in axes:
        ax.axvline(result.onset_idx / fs, color="green", linestyle="--", label="onset")
        color = "red" if result.offset_source == "detected" else "orange"
        ax.axvline(result.offset_idx / fs, color=color, linestyle="--",
                    label=f"offset ({result.offset_source})")
        for b in result.other_bouts:
            ax.axvspan(b.start_idx / fs, b.end_idx / fs, color="purple", alpha=0.15)
        ax.grid(alpha=0.3)
    axes[1].axhline(result.threshold, color="gray", linestyle=":", linewidth=1, label="threshold")
    axes[0].legend(loc="upper right", fontsize=8)
    axes[1].legend(loc="upper right", fontsize=8)
    plt.tight_layout()
    plt.savefig(plot_dir / f"{sheet_name}.png", dpi=110)
    plt.close(fig)


def main():
    here = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", type=Path, default=here / "SWEAT_DTG IMU Summary Data_ALL.xlsx")
    p.add_argument("--trial-times", type=Path, default=here / "SWEAT_DTG Trial times_ALL.xlsx")
    p.add_argument("--outdir", type=Path, default=here / "output")
    p.add_argument("--fs", type=float, default=FS_DEFAULT, help="nominal sample rate (Hz)")
    p.add_argument("--k", type=float, default=2.0, help="baseline_mean + k*baseline_sd threshold multiplier")
    p.add_argument("--plot-n", type=int, default=10, help="number of diagnostic plots to save (0 to disable)")
    args = p.parse_args()

    process_all(args.source, args.trial_times, args.outdir, k=args.k, fs=args.fs, plot_n=args.plot_n)


if __name__ == "__main__":
    main()
