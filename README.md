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
outputs go to `output/`:

- `output/imu_samples_tagged.parquet` — every sample, tagged with
  participant/condition/phase (regenerate from source — gitignored, large).
- `output/trial_summary.csv` — per-trial detection results and QC flags
  (committed).

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

## Gait metrics: `scripts/compute_gait_metrics.py`

With trial boundaries settled by the review above, this computes walking
speed and per-axis sway for 147 trials (the 2 confirmed-unusable trials
excluded; run via `python scripts/compute_gait_metrics.py`).

**Segmentation** — per trial, using the *reviewed* method choice for that
specific trial (not a blanket rule, since review found this genuinely
varies — see the hybrid finding above), and the turn location the reviewer
corrected where applicable, else the auto-detected one:

- `outbound` = `[start_event, turn − 2s]`
- `turn`     = `[turn − 2s, turn + 2s]` — **excluded** from sway metrics
- `return`   = `[turn + 2s, stop_event]`

**Walking speed** = the protocol's known 3 m walked distance ÷ segment
duration — not integrated from acceleration, which drifts within seconds
on a single IMU with no zero-velocity updates (see the detection section
above for the same reasoning).

**Per-axis sway** (mean, SD, range) computed separately for each segment,
on the raw sensor axes per the study's mounting convention: `Acc_X` =
vertical, `Acc_Y` = lateral (M/L), `Acc_Z` = forward/backward (AP). Cross-
checked against quiet-stance data: `Acc_Y` sits near zero at rest (no
lateral tilt, as expected) while gravity splits between `Acc_X`/`Acc_Z`,
consistent with the belt sitting at a slight forward/backward tilt rather
than perfectly level — `Acc_X` still carries the majority of gravity, so
the vertical/AP labels hold, they just aren't perfectly decoupled.

### Output files (one row per trial in all three — none of it aggregated)

- **`output/gait_metrics.csv`** — the primary output, in pipeline order.
  Columns: `sheet`/`participant`/`condition`; `start_choice`/`stop_choice`
  (which reviewed method was used); `turn_s` and the four segment
  boundary times; `outbound_duration_s`/`return_duration_s`; `outbound_
  speed_mps`/`return_speed_mps`; then per segment × per axis (`vertical`/
  `lateral`/`ap`) × `mean`/`sd`/`range` (e.g. `outbound_vertical_sd`).
- **`output/gait_metrics_by_participant.csv`** — identical rows, sorted by
  participant then condition (`DTG_TRIAL`→`DTG1`→`DTG2`→`DTG3`), for
  reading one person's 4 trials together.
- **`output/gait_metrics_by_condition.csv`** — identical rows, sorted by
  condition then participant, for reading one condition across everyone.

### What the numbers show so far

Averaging `outbound_speed_mps`/`return_speed_mps` and the three `_sd`
columns per condition across all participants:

| Condition | Speed out (m/s) | Speed back | Vertical SD out | Vertical SD back |
|---|---|---|---|---|
| DTG_TRIAL | 0.385 | 0.355 | 0.801 | 0.829 |
| DTG1 | 0.415 | 0.399 | 0.913 | 0.885 |
| DTG2 | 0.429 | 0.402 | 0.932 | 0.918 |
| DTG3 | 0.454 | 0.430 | 0.984 | 1.013 |

Two patterns worth noting before drawing conclusions:

1. **Speed and sway rise together, monotonically, across all four repeated
   trials** (`DTG_TRIAL`→`DTG3`). Since `DTG_TRIAL`→`DTG1` is same-day
   practice, this looks more like a **practice/familiarity effect**
   (walking faster, with more natural vertical oscillation, as the task
   becomes routine) than fatigue-driven instability — a genuine
   hydration/fatigue effect would be expected to decouple sway from speed,
   not track it.
2. **Outbound is consistently faster than return** in every condition —
   matches earlier notes flagging outbound/return asymmetry as worth a
   formal paired test rather than eyeballing the means.

Per-participant walking speed varies about 2.5× between the slowest and
fastest individuals, and tracks each person's own sway fairly closely
(the fastest participant is also the swayest; the slowest is also the
steadiest) — worth analyzing jointly rather than as independent outcomes.

## File map

```
scripts/ingest_imu.py                       Ingestion + both detection methods
scripts/compute_gait_metrics.py             Walking speed + per-axis sway from reviewed trial windows
output/trial_summary.csv                    Per-trial detection results
output/gait_metrics.csv                     Per-trial speed + sway (147 rows, pipeline order)
output/gait_metrics_by_participant.csv      Same rows, sorted by participant
output/gait_metrics_by_condition.csv        Same rows, sorted by condition
output/imu_samples_tagged.parquet           Tagged sample-level data (regenerate; gitignored — large)
review/onset_review_labels.csv              Human review verdicts — the durable output of that phase
requirements.txt                            Python dependencies
```

## Suggested next steps

1. Formal statistical tests on `output/gait_metrics.csv`: a repeated-
   measures trend test across the 4 conditions, and a paired outbound-vs-
   return test, to confirm the patterns above rather than reading them off
   the means.
2. Decide whether the practice-effect trend needs a within-subject
   correction (e.g. using `DTG_TRIAL` as each participant's own baseline)
   before comparing `DTG1`/`DTG2`/`DTG3` for a hydration/fatigue effect.
3. Consider the supplementary metrics noted earlier but not yet
   implemented (step cadence/stride-time CV, step regularity via
   autocorrelation, turn duration) if the axis-level sway metrics alone
   don't separate the conditions cleanly.
