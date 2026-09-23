# How to label an episode

The labels in `episodes.csv` are the ground truth the agent is scored against.
Everything the north star metric says about *correctness* rests on them, so this
file exists to make the judgement repeatable rather than a mood on the day.

Written before labelling starts, on purpose. A protocol written afterwards is a
description of what was already done.

## The question you are answering

> Given only what was published **before** this episode began, what made
> Portugal and Spain price apart during this window?

Two things in that sentence do work.

**Given only what was published before.** The sheet already enforces it: the
notices shown for an episode are the ones published before it started. Do not go
and look up what was later found to have happened. The agent could not see it,
so scoring the agent against it measures nothing useful.

**What made them price apart**, not what the agent said. Do not read the
explanation while labelling. If the label is influenced by the output, the metric
measures the system agreeing with itself, which is exactly what happened on the
first pass through this sheet.

## The order to decide in

Work down the list. The first one that fits is the answer.

### 1. Is this a real event at all?

Look at `peak_abs_spread` and `severity`.

The decoupling test is `|price_pt - price_es| > 0.01 EUR/MWh`. A spread of a few
cents is two prices that are the same number rounded differently, not a market
event with a cause.

> **`threshold_artifact`** when the spread is at or near the epsilon and nothing
> else about the episode suggests a real separation.

Pick your own line for "near the epsilon", write it here, and apply it to every
episode. A line applied consistently is defensible even if somebody would have
drawn it elsewhere; a line that moves is not.

**The line used in this sheet: _____ EUR/MWh.**

### 2. Was the interconnection the binding constraint?

Look at `share_saturated` and `mean_utilisation`.

If the border was not full, the two zones could still trade, and something else
set the price difference: the generation mix, demand, a bidding outcome. That is
a real event with a real cause, and the cause is not the interconnection.

> **`not_saturated`** when the prices separated with headroom on the border.

This label never appeared in the first pass. That alone was a sign something was
wrong: a market that never once prices apart with an open border would be a
strange market.

### 3. Does a published notice account for the capacity?

Only now look at the notices block.

The comparison that matters is `tightest_available_mw` against
`min_capacity_mw`, which the sheet does for you as `unexplained_mw`:

- **Negative or near zero.** The notice is at least as tight as the border
  figure, so the outage plausibly accounts for the reduction. Go to step 4.
- **Clearly positive.** The notice permits *more* capacity than the border
  actually had. It does not explain the collapse, whatever else it says.

> **`saturation_no_notice`** when the border was at its limit and no notice in
> force accounts for the capacity it had. This includes the case of zero notices,
> and the case of a notice that leaves a large gap unexplained.

A78 is asset level and A61 is the net border figure after the operator's security
assessment, so a small gap either way is normal. A gap of many hundreds of MW is
not.

### 4. Planned or unplanned?

Read `tightest_status`.

> **`saturation_planned`** for A53, planned maintenance.
> **`saturation_unplanned`** for A54, an unplanned outage.

This is the one step that is a lookup rather than a judgement. It is last for a
reason: reaching it means you have already decided that a notice explains the
capacity.

### 5. Otherwise

> **`unclear`** when the evidence in front of you does not settle it.

Using this is not giving up. A metric reported as "correct on 44 of the 51
episodes a human could settle, with 8 the evidence does not settle" is stronger
than one that pretends every case was clean, because the second one invites the
question of what you did with the hard ones.

If `unclear` is more than about a fifth of the sheet, the problem is probably the
evidence rather than the labeller, and that is a finding worth writing down.

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