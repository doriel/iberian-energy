# Cause vocabulary

Pick exactly one value for `true_cause`. Set `confidence` to high,
medium or low, and use `notes` for anything that made it hard.

A label that disagrees with `candidate_cause` is the most valuable
row in the sheet. Write down why in `notes`.

- `saturation_planned`: Capacity unusually low, a planned outage is consistent with it
- `saturation_unplanned`: Capacity unusually low, an unplanned outage is consistent
- `saturation_no_notice`: Capacity unusually low, no notice accounts for it
- `saturation_ordinary_capacity`: Border full at ordinary capacity, nothing reduced it
- `not_saturated`: Priced apart with headroom on the border, cause unknown
- `threshold_artifact`: Spread at the rounding epsilon, not a real event
- `unclear`: The available evidence does not settle it
