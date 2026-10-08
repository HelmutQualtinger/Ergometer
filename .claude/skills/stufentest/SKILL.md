---
name: stufentest
description: >
  Evaluate a step test (Stufentest) recorded by the ergometer program and present it as a
  Plotly page: maximum power, estimated VO2max, heart rate against power with a linear fit,
  and where the result lies in the measured distributions of men of the same age (quantiles
  from the FRIEND and SHIP reference data). Use this whenever the user asks to analyse,
  evaluate or judge a step test or a log from logs/, asks about their fitness, VO2max or
  percentile from an ergometer recording, or says things like "Stufentest auswerten",
  "werte den Test aus", "wie fit bin ich", "Analyse des letzten Logs" - even if they do
  not name the script.
---

# Stufentest

`stufentest.py` in the repo root does the whole evaluation deterministically. Your job is to
give it the right inputs, run it, open the page and report the result. Do not recompute the
numbers by hand and do not edit the generated HTML; if something about the evaluation should
change, change the script.

## Inputs

The script needs facts about the rider that are not in the log:

- `--age` (years) and `--weight` (kg): required.
- `--height` (cm): optional. It only decides the BMI group (up to 25 / above 25) of the German
  reference data; without it the script assumes "above 25" from 81 kg on and says so on the page.
- `--betablocker`: pass it if the rider takes a beta blocker. The page then stops judging the
  heart rate (flat slope, low maximum, "220 - age"), because the medication causes exactly that.
  Leaving the flag out for such a rider produces a wrong assessment, so check rather than guess.

- `--pdf`: also writes `logs/stufentest-<timestamp>.pdf`, a compact two-column A4 portrait
  version of the page (printed with headless Google Chrome). Pass it when the user wants a PDF
  or something to print or send.

Take these from what the user said in this conversation or from memory. If age or weight is
unknown, ask once; weight changes, so prefer a value the user gave recently over an old one.

## Run

```bash
python3 stufentest.py --age 50 --weight 80 --betablocker          # newest log in logs/
python3 stufentest.py logs/log-26-10-08-00-36-23.csv --age 50 --weight 80 --height 180
```

It needs only the Python standard library. It writes `logs/stufentest-<timestamp>.html` next
to the log and prints the stages, the fit and the percentiles. Then open the page for the user:

```bash
open logs/stufentest-<timestamp>.html
```

The page loads Plotly from a CDN and works from `file://`.

## Check before reporting

Look at the printed stage list, because the script evaluates whatever log it is given:

- A step test has several stages of equal length with rising targets. If the newest log is an
  interval session or a 30-second trial, say so and ask which log was the step test instead of
  presenting numbers that mean nothing.
- "no pulse" on a stage means the sensor delivered nothing there. The fit then rests on fewer
  stages; with fewer than three it is not worth much - tell the user.
- The maximum power is the last full stage plus the time share of the one that was broken off.
  It is only a maximum if the rider went to exhaustion; the log cannot tell.

## Report

Answer in the user's language, lead with the result, and keep the caveats that change how the
numbers should be read:

- maximum power in W and W/kg, and its percentile among men of the same age decade;
- estimated VO2max in ml/kg/min with its percentile, and the German group's median and P95;
- the fit (pulse = a + b x power) and r;
- which comparisons are measured quantiles and which are computed: FRIEND publishes VO2peak
  deciles, but only mean and SD for the peak workload, so that percentile assumes a normal
  distribution; the rider's VO2max is estimated from the power (ACSM), not measured;
- with a beta blocker: the heart rate says nothing about fitness or effort.

These are numbers from a home test. Say what they show; do not present them as a medical
finding, and do not turn them into training or medication advice the user did not ask for.

## Reference data

Both sets are in `stufentest.py` with their sources, and both cover men only - for a woman the
percentiles would be wrong, so say that instead of running the comparison.

- FRIEND registry (USA), cycle ergometry with gas analysis, men without cardiovascular
  disease, per age decade 20-89: Kaminsky et al., Mayo Clin Proc 2022, doi 10.1016/j.mayocp.2021.08.020.
- SHIP (Germany), 534 healthy adults, quantile regression for P5, median and P95 by age group,
  sex and BMI group: Koch et al., Eur Respir J 2009, doi 10.1183/09031936.00074208.

The results stay out of git: `logs/` holds personal recordings and the repository is public.
