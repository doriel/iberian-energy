# How to label an episode

The labels in `episodes.csv` are the ground truth the agent is scored against.
Everything the north star metric says about *correctness* rests on them, so this
file exists to make the judgement repeatable rather than a mood on the day.

Written before labelling starts, on purpose. A protocol written afterwards is a
description of what was already done.

## The question you are answering

> Given only what was published **before** this episode began, is the published
> evidence consistent with an outage having reduced the border, or was the border
> simply full at an ordinary level?

Note what that question does **not** ask. It does not ask which outage caused the
capacity to be what it was, and it cannot: the operator recomputes the net
transfer capacity every day from the whole system state, and the reasoning is
never published. Nothing in the public record says "this maintenance is why the
border was 3195 MW today". A label claiming otherwise would be a guess wearing a
ground truth's clothes.

What the evidence does support is whether the capacity was unusual, and whether
anything published is consistent with it being unusual. That is a weaker claim
and it is the one the platform can honestly make.

Two things in that sentence do work.

**Given only what was published before.** The sheet already enforces it: the
notices shown for an episode are the ones published before it started. Do not go
and look up what was later found to have happened. The agent could not see it,
so scoring the agent against it measures nothing useful.

**What the evidence supports**, not what the agent said. Do not read the
explanation while labelling. If the label is influenced by the output, the metric
measures the system agreeing with itself, which is exactly what happened on the
first pass through this sheet.

## What is on the screen, and what each number means

The tool prints readable labels; the sheet stores column names. They are the
same things under two names, and the rest of this file uses the column names.

| On screen | Column | What it is |
|---|---|---|
| `Peak spread 109.84 EUR/MWh, PT pays` | `peak_abs_spread` | The largest gap between the two zones during the episode, and which side paid more. Size of the event. |
| `Severity severe` | `severity` | The band that gap falls in: minor under 5, moderate 5 to 20, severe above 20 EUR/MWh. |
| `Extra cost 1,853,272 EUR` | `extra_cost_eur` | What the gap cost Portuguese consumption over the episode. A materiality figure, not a cause. |
| `Saturated in 1.0 of the episode's intervals` | `share_saturated` | The fraction of quarter hours in which the flow filled the available capacity. 1.0 means full the whole time, 0.0 means never. |
| `Mean use 1.0` | `mean_utilisation` | Average flow divided by capacity. Near 1.0 means the border was working at its ceiling, well below means there was room. |
| `Min capacity 3195.0 MW` | `min_capacity_mw` | The lowest capacity the operator published for this border during the episode. What the border was actually allowed to carry. |
| `AT 3 400/220 SRM` | `tightest_asset` | The asset under the most restrictive notice in force. Empty when the operator published no name. |
| `2800.0 MW` | `tightest_available_mw` | What that notice says **remains available** on that asset. Not the amount taken out of service. The two get read the wrong way round constantly. |
| `planned` | `tightest_status` | Whether that notice is planned maintenance (A53) or an unplanned outage (A54). |
| `published 2026-07-27` | `tightest_published` | When the notice was published. Always before the episode: the sheet only shows notices that existed in time. |
| `12 notice(s)` | `notices` | How many notices were in force and published in time. A count, nothing more. |
| `Notice is 395 MW tighter...` or `Unexplained 900 MW` | `unexplained_mw` | `tightest_available_mw` minus `min_capacity_mw`. Positive means the notice allows more than the border had, so it does not account for it. Negative is necessary for consistency but not sufficient: see the next row. |
| `Percentile 3 of every quarter hour in the window` | `capacity_percentile` | Where this episode's lowest capacity sits among every quarter hour in the window. Low means the border was carrying unusually little and something reduced it. At or above 25 it is an ordinary level and there is nothing to explain. The single most important number on the screen. |
| `That asset is 88% of the border figure` | `notice_share_of_border` | `tightest_available_mw` divided by `min_capacity_mw`. Near 1 the asset under notice is most of the border, so its outage plausibly set the limit. Well under 1 the border had as much again elsewhere, so that notice alone did not constrain it. |
| `(the median quarter hour is 5,130 MW)` | `median_capacity_mw` | The middle of the window's capacity distribution, for scale. There is no published normal for this border, so this is the closest thing to one. |

## A worked example

Invented numbers, not one of the episodes in the sheet. A real episode worked
through here would become the answer for every episode that looks like it, which
is the failure this protocol exists to prevent.

```
  Peak spread   42.10 EUR/MWh, PT pays
  Severity      severe

  Border
    Saturated in  1.0 of the episode's intervals
    Mean use      1.0
    Min capacity  5400.0 MW
    Percentile    61 of every quarter hour in the window
                  (the median quarter hour is 5,130 MW)

  Notices available before it began
    6 notice(s). Tightest:
      Falagueira-Cedillo  2800.0 MW  planned, published 2026-06-02
    That asset is 52% of the border figure.
    Notice is 2,600 MW tighter than the border figure
```

Working down the list:

1. **Real event?** 42 EUR/MWh. Yes. Not an artifact.
2. **Border full?** 1.0. Yes, as always.
3. **Anything to explain?** Percentile 61. This border carries this much
   routinely, more than the median quarter hour. Nothing was taken away. →
   **`saturation_ordinary_capacity`**. The price gap is real and expensive, and
   its cause is that the interconnection is smaller than the flow the price
   difference would justify.
4. Steps 4 and 5 are never reached. There is a planned notice sitting in the
   evidence and it is irrelevant: that asset is half the border, and the border
   was at an ordinary level anyway.

Change one number and the answer changes:

- `Percentile 4` instead of 61, with the same notice: the border was carrying
  unusually little, so go to step 4. The asset is 52% of the border, so that
  notice alone did not constrain it → **`saturation_no_notice`**.
- `Percentile 4`, and the notice reads `That asset is 91% of the border figure`:
  the asset under maintenance is nearly the whole border →
  **`saturation_planned`**.
- `Unexplained 700 MW` instead of the notice being tighter: the notice permits
  more than the border carried, so whatever cut it, this was not it →
  **`saturation_no_notice`**, whatever the percentile says.

## The order to decide in

Work down the list. The first one that fits is the answer.

### 1. Is this a real event at all?

`peak_abs_spread`. The decoupling test is a difference above 0.01 EUR/MWh, so a
spread of a few cents is two prices that are the same number rounded
differently.

> **`threshold_artifact`** when the spread is at or near the epsilon.

**The line used in this sheet: _____ EUR/MWh.** Pick it once, write it here,
apply it to every episode.

### 2. Was the border full?

`share_saturated`. In this dataset it is 1.0 for every episode, and that is
expected rather than suspicious: zones decouple precisely because the
interconnection binds. If it were not full, the coupling algorithm would have
equalised the prices.

> **`not_saturated`** if you ever see one that is not. It should not happen, and
> if it does the finding is about the pipeline, not the market.

### 3. Was there anything to explain?

`capacity_percentile`: where this episode's lowest capacity sits among every
quarter hour in the window.

This is the step the sheet used to be missing, and without it the rest is
guesswork. A border that is full at a level it reaches routinely has not been
reduced by anything. It is full because the price difference would justify more
flow than the interconnection can carry, which is a cause in its own right and
the most common one here.

- **At or above the 25th percentile**: an ordinary level for this border.

> **`saturation_ordinary_capacity`**. Nothing was taken away, so no outage
> explains it. Notices in the evidence are irrelevant here, and this is the trap
> the first pass fell into: a planned outage sitting in the list does not make
> the answer `saturation_planned`.

- **Below the 25th percentile**: the border was carrying unusually little.
  Something reduced it. Go to step 4.

### 4. Is a notice consistent with the reduction?

Two numbers, and they answer different halves of it.

`unexplained_mw`, which is the notice's remaining capacity minus the border
figure:

- **Positive** means the notice permits more than the border actually carried,
  so whatever cut the capacity, it was not this.

`notice_share_of_border`, which is how much of the border that one asset's
remaining capacity amounts to:

- **Near 1** means the asset under notice is most of the border, so its outage
  plausibly set the limit.
- **Well below 1**, say under 0.7, means the border had roughly as much again
  elsewhere, and that notice alone did not constrain it.

Both are heuristics. The border total is not the sum of its assets and the
security assessment behind it is not published, which is why this step decides
*consistency* and not cause.

> **`saturation_no_notice`** when there are no notices, or the tightest one
> permits more than the border carried, or it covers only a fraction of it.

### 5. Planned or unplanned?

`tightest_status`. A53 is planned maintenance, A54 unplanned.

> **`saturation_planned`** or **`saturation_unplanned`**.

A lookup rather than a judgement, and last on purpose: reaching it means you have
already decided both that something reduced the border and that this notice is
consistent with it.

### 6. Otherwise

> **`unclear`** when the evidence in front of you does not settle it.

Not giving up. "Correct on 44 of the 51 a human could settle, with 8 the evidence
does not settle" is a stronger sentence than one pretending every case was clean.

## Confidence and notes

`confidence` is about *your* certainty, not the size of the spread.

- **high**: the evidence points one way and you would defend it to a grid
  analyst.
- **medium**: you believe it, and a reasonable person could land elsewhere.
- **low**: you picked the least bad option.

A sheet where every row is `high` is not a confident labeller, it is a labeller
who is not thinking. Expect a spread.

`notes` are optional except in one case: **when you disagree with what the rules
proposed, write a sentence saying why.** Those rows are the most valuable in the
file. They are where the rules are wrong, and they are what you will quote when
somebody asks how you know the evaluation is not circular.

## How to run a sitting

```bash
python scripts/label_episodes.py --limit 15
```

Fifteen at a time, then stop. Judgement degrades with fatigue and the failure
mode is not random error, it is agreeing with whatever is in front of you.

The tool does not show the rules' answer before it asks. It shows it after, as
feedback. If you want to review the rules rather than the episodes, pass
`--show-candidate`, and do not use those rows as ground truth.

## What a healthy sheet looks like

Not balanced. Real markets are lumpy: one asset under one long planned outage can
dominate a month. Balance across categories is a sign of generated labels, not of
good sampling.

What it should have: a mix of confidences, some notes, at least a few `unclear`,
and disagreement with the rules somewhere in the low tens of percent. Zero
disagreement means either the rules are perfect, which they are not, or the
labels came from the rules.