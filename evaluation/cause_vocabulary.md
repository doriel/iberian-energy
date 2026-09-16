# Cause vocabulary

Pick exactly one value for `true_cause`. Set `confidence` to high,
medium or low, and use `notes` for anything that made it hard.

A label that disagrees with `candidate_cause` is the most valuable
row in the sheet. Write down why in `notes`.

- `saturation_planned`: Border at its limit, explained by a planned outage notice
- `saturation_unplanned`: Border at its limit, explained by an unplanned outage
- `saturation_no_notice`: Border at its limit, no notice accounts for it
- `not_saturated`: Priced apart with headroom on the border, cause unknown
- `threshold_artifact`: Spread at the rounding epsilon, not a real event
- `unclear`: The available evidence does not settle it
