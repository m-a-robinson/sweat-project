# SWEAT-DTG Pelvis IMU Analysis

Gait-sway analysis of the dual-task-gait (DTG) trials from the SWEAT study: a
pelvis-mounted IMU recorded during a 3 m walk-with-turn, performed by
jockeys under three hydration/fatigue conditions. This document summarizes
the analysis work done so far and the decisions behind it, as a reference
for picking the work back up.

## Study design

- **Participants**: SWEAT-001 to SWEAT-039 (SWEAT-013 excluded — no IMU
  data collected).
- **Conditions per participant**: `DTG_TRIAL` (practice), `DTG1` (baseline),
  `DTG2` (post), `DTG3` (post-24h) — up to 4 trials each, 149 trials total
  after exclusions (SWEAT-002/004 opted out of trial 3; SWEAT-032 didn't
  complete trial 3).
- **Task**: walk 3 m, turn, walk back, timed by stopwatch (`SWEAT_DTG Trial
  times_ALL.xlsx`).
- **Sensor**: Xsens MTw Awinda on the pelvis, ~100 Hz. The export
  (`SWEAT_DTG IMU Summary Data_ALL.xlsx`, one sheet per participant×condition)
  contains `Acc_X/Y/Z` (accelerometer, m/s²) and `Roll/Pitch/Yaw` (fused
  orientation, degrees) — **no raw gyroscope channel is exported**.

## Data quality findings

- Sample rate confirmed at ~100 Hz from continuous `PacketCounter`
  increments cross-checked against logged trial durations.
- Sheets routinely contain **far more than the timed walk**: recordings
  ranging from a few extra seconds up to one case of 885 seconds (nearly
  15 minutes) for a walk logged at 21 seconds. There is no independent
  timestamp marking gait start/end in the file — only the participant
  standing still for calibration before the walk, and sometimes (not
  always) afterward.
- Several trials contain more than one distinct movement bout in the same
  sheet (repeated attempts, equipment adjustment), matching notes on
  specific participants (e.g. P1: "repeated rep... recorded = 21 seconds").

## Pipeline: `scripts/ingest_imu.py`

Parses all 149 sheets into a tidy, tagged dataset and locates each trial's
start/end within the recording. Run via `python scripts/ingest_imu.py`;
outputs go to `output/` (gitignored — regenerate from source, don't rely on
committed copies):

- `output/imu_samples_tagged.parquet` — every sample, tagged with
  participant/condition/phase.
- `output/trial_summary.csv` — per-trial detection results and QC flags.

### Two onset/offset detection methods, and why neither alone was enough

**Amplitude/threshold method** (`detect_quiet_periods`): rolling SD of
acceleration magnitude vs. a `mean + 2×SD` baseline (the "2SD change" rule),
segmented into movement bouts, longest bout picked as the trial. Works well
when the recording is close to the timed walk, but a threshold has no way
to distinguish "the real walk plus some idle time" from "the real walk" —
so on badly padded recordings it either grabs way too much (idle time
included) or, if a repeated attempt or equipment bump has a bigger
amplitude blip, the wrong bout entirely.

**Turn-anchored method** (`detect_trial_window`): since the DTG protocol is
a walk-with-turn, the turn should sit near the midpoint of the timed walk.
The turn is found as the peak pseudo-angular-rate sample (`gyro_mag`,
differentiated from the orientation channels since no raw gyro exists)
inside the amplitude method's bout, with a margin trimmed off each end of
the bout first (so a sensor bump at the very start/end of a bout isn't
mistaken for the turn). The trial window is then
`turn_time ± logged_duration/2`. Validated against the logged Trial Times:
this took trials landing within 2s of the logged duration from 55% (pure
threshold) to 90%, and within 5s from 84% to 97%.

Both methods' numbers are kept side by side in `trial_summary.csv`
(`onset_s`/`offset_s` = turn-anchored, `bout_onset_s`/`bout_offset_s` =
threshold), because **neither method's automatic pick was trusted on its
own** — see the review process below.

## Human-in-the-loop review

Automatic turn detection can be wrong when a bout contains more than one
large angular-rate spike (a stumble, a knock, a second attempt) with a
bigger amplitude than the real turn — amplitude alone can't tell them
apart. Rather than keep hand-tuning heuristics against a handful of
inspected examples, all 149 trials were visually reviewed by the study
lead in an interactive tool (built as a self-saving Claude Artifact, not
checked into this repo — the durable output is `review/onset_review_labels.csv`).

For each trial, both the auto-detected turn-anchored window and the
threshold window were plotted against the acceleration and pseudo-gyro
traces, and the reviewer judged, separately for the **start** event and the
**stop** event: turn-anchored, threshold, or unclear. Where a trial was
unclear because the auto-detected turn was in the wrong place, the tool
allowed dragging directly on the chart to mark the true turn location,
which recomputed the turn-anchored window live and reset that trial for
re-judgment.

### Result: 149/149 trials reviewed

| Event | Turn-anchored | Threshold | Unclear |
|---|---|---|---|
| **Start** | 66 | 81 | 2 |
| **Stop** | 142 | 5 | 2 |

**15 trials** needed a manual turn correction (the auto-detected turn was
in the wrong place — usually a larger-but-irrelevant spike elsewhere in the
same bout). After correction, those 15 judged turn-anchored correct on
stop in all 15 cases and on start in 11/15 — i.e. once the turn itself is
right, turn-anchoring is reliable; the weak point was the turn-*picking*
heuristic (biggest spike ≠ real turn), not the anchoring approach itself.

### Key finding: the two methods split by event, not by trial

Across the full set, **77/149 trials (52%) judged threshold-correct for
start and turn-anchored-correct for stop** — a clear majority pattern, well
ahead of any other combination (turn-anchored on both: 65; threshold on
both: 4). This makes sense post hoc: the threshold crossing is a direct,
local measurement of "when does the accelerometer variance step up" and
that's exactly what a gait *onset* looks like, whereas offset is unreliable
for threshold (many recordings never return to a clean quiet baseline
after the walk — the IMU just keeps running) but turn-anchoring sidesteps
that entirely by working forward from a fixed point rather than needing a
quiet tail to detect.

**Recommendation for the next processing pass**: use the threshold
crossing for the start event and the turn-anchored estimate for the stop
event — a hybrid, not a single winner-take-all method.

### Remaining open items

- **2 trials still unclear** after review, with reasons recorded in
  `review/onset_review_labels.csv` (`note` column):
  - `SWEAT_005DTG2` — turn-anchored window is not symmetrical either side
    of the detected turn (worth a closer look at whether the DTG protocol
    assumption of a midpoint turn held for this trial).
  - `SWEAT_035DTG1` — signal data cuts out before 10s; likely unusable.
- Two trials (`SWEAT_007DTG3`, `SWEAT_008DTG2`) had grossly over-long
  recordings (230s and 886s respectively, against ~20s logged walks) that
  made the auto-detected turn meaningless; their charts were cropped to the
  first 40s for review and both were re-judged against the cropped view
  (now included in the 149/149 figure above).

## File map

```
scripts/ingest_imu.py              Ingestion + both detection methods
output/trial_summary.csv           Per-trial detection results (regenerate; gitignored)
output/imu_samples_tagged.parquet  Tagged sample-level data (regenerate; gitignored)
review/onset_review_labels.csv     Human review verdicts — the durable output of this phase
requirements.txt                   Python dependencies
```

## Suggested next steps

1. Implement the hybrid start=threshold / stop=turn-anchored window as the
   production trial-boundary method in `ingest_imu.py`, using
   `review/onset_review_labels.csv` as the validation set.
2. Resolve or exclude the 2 remaining unclear trials.
3. With trial windows finalized, move on to the actual gait/sway metrics
   (trunk acceleration RMS, step cadence variability, turn duration,
   outbound/return asymmetry — see the original analysis plan) and the
   between-condition (baseline/post/post-24h) comparison.
