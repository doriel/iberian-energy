"""The guarantee that the explanation layer rests on.

The design says the model phrases retrieved facts and never produces a figure
of its own. A prompt cannot guarantee that, so it is enforced after generation.
These tests pin down both failure directions: an invented number must fail, and
a faithful rendering of a real number must not.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.agent.facts import Fact, FactSheet, episode_facts  # noqa: E402
from iberian.agent.verify import extract_claims, verify  # noqa: E402

START = datetime(2026, 8, 18, 7, 45, tzinfo=timezone.utc)
END = datetime(2026, 8, 18, 15, 15, tzinfo=timezone.utc)


def sheet(*facts: Fact, caveats: list[str] | None = None) -> FactSheet:
    return FactSheet(
        subject="test episode",
        start_utc=START,
        end_utc=END,
        market_day=date(2026, 8, 18),
        facts=list(facts),
        caveats=caveats or [],
    )


def standard() -> FactSheet:
    return sheet(
        Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead"),
        Fact("min_border_capacity", 3195.0, "MW", "ENTSO-E A61 day-ahead capacity"),
        Fact("share_of_intervals_saturated", 1.0, "", "A09 flow against A61 capacity"),
        Fact("constrained_asset", "Alcochete-Palmela", "", "ENTSO-E A78"),
    )


def test_an_invented_number_fails():
    """The whole point. 4200 MW was never retrieved."""
    text = (
        "Portugal paid 109.84 EUR/MWh more [ENTSO-E A44] while the border was "
        "limited to 4200 MW."
    )
    verdict = verify(text, standard())

    assert not verdict.ok
    assert [claim.value for claim in verdict.unsupported] == [4200.0]


def test_the_retrieved_numbers_pass():
    text = (
        "Portugal paid up to 109.84 EUR/MWh more than Spain [ENTSO-E A44 "
        "day-ahead] while day-ahead capacity fell to 3,195 MW."
    )
    assert verify(text, standard()).ok


def test_thousands_separators_are_the_same_number():
    text = "Capacity was 3,195 MW [ENTSO-E A61]."
    assert verify(text, standard()).ok


def test_a_share_written_as_a_percentage_passes():
    """1.0 rendered as 100% is not an invention."""
    text = "The border was full in 100% of intervals [A09 flow against A61 capacity]."
    assert verify(text, standard()).ok


def test_a_partial_share_written_as_a_percentage_passes():
    facts = sheet(
        Fact("share_of_intervals_saturated", 0.37, "", "A09 against A61"),
        Fact("peak_premium", 12.93, "EUR/MWh", "ENTSO-E A44"),
    )
    assert verify("Split in 37% of intervals [A09 against A61].", facts).ok


def test_rounding_to_the_precision_written_is_faithful():
    """"roughly 3,200 MW" is a fair reading of 3195."""
    text = "Capacity fell to roughly 3,200 MW [ENTSO-E A61]."
    assert verify(text, standard()).ok


def test_a_different_number_is_not_rounding():
    """3,500 is not 3,195, and accepting it would make the check theatre."""
    text = "Capacity fell to 3,500 MW [ENTSO-E A61]."
    verdict = verify(text, standard())
    assert not verdict.ok
    assert verdict.unsupported[0].value == 3500.0


def test_dates_and_times_are_not_treated_as_measurements():
    text = (
        "Between 2026-08-18 07:45Z and 15:15Z the premium reached 109.84 EUR/MWh "
        "[ENTSO-E A44]."
    )
    assert verify(text, standard()).ok


def test_a_date_written_out_in_prose_is_not_a_measurement():
    """"published on 25 June 2026" cost us 19 rejections of faithful text.

    The date must not be read as the numbers 25 and 2026. It must still be
    checked as a date, which is a separate question, so this asserts only that
    the numeric check leaves it alone.
    """
    facts = sheet(
        Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead"),
        Fact("notice_published", "2026-06-25", "", "ENTSO-E A78"),
    )
    text = (
        "The notice was published on 25 June 2026 and the premium reached "
        "109.84 EUR/MWh [ENTSO-E A44]."
    )
    verdict = verify(text, facts)
    assert verdict.ok
    assert all(claim.value != 2026 for claim in verdict.claims)


def test_every_prose_date_shape_is_read_as_a_date():
    """The shapes the models actually write, each one in the evidence.

    Previously this asserted that any prose date passed, which is what let
    three explanations in a run of forty-five state the wrong day. The shapes
    still have to be recognised; what changed is that recognising one now
    means checking it.
    """
    for written in (
        "on 18 August 2026",
        "on 18 August",
        "published August 18, 2026",
        "during August 2026",
        "from 09:30Z to 09:45Z on 18 August 2026",
    ):
        text = f"The premium was 109.84 EUR/MWh [ENTSO-E A44], {written}."
        assert verify(text, standard()).ok, written


# --- dates, which are claims too --------------------------------------------


def test_the_wrong_day_is_rejected():
    """The bug this check exists for.

    Three explanations out of forty-five opened with a date that was not the
    episode's. Every figure in them was retrieved, so the numeric check passed
    them, and the one assertion a reader could verify unaided was false.
    """
    text = (
        "The market split on 2 September 2026 with a premium of 109.84 "
        "EUR/MWh [ENTSO-E A44]."
    )
    verdict = verify(text, standard())
    assert not verdict.ok
    assert [claim.text for claim in verdict.wrong_dates] == ["2 September 2026"]


def test_the_wrong_day_in_iso_is_rejected_too():
    text = "On 2026-09-03 the premium reached 109.84 EUR/MWh [ENTSO-E A44]."
    assert not verify(text, standard()).ok


def test_the_market_day_passes():
    text = "On 2026-08-18 the premium reached 109.84 EUR/MWh [ENTSO-E A44]."
    assert verify(text, standard()).ok


def test_a_retrieved_publication_date_passes():
    # The agent is required to say when a notice was published, so the date in
    # a fact has to be quotable.
    facts = sheet(
        Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead"),
        Fact("notice_published", "2026-07-23", "", "ENTSO-E A78"),
    )
    text = (
        "A notice published on 2026-07-23 was in force while the premium "
        "reached 109.84 EUR/MWh [ENTSO-E A44, ENTSO-E A78]."
    )
    assert verify(text, facts).ok


def test_a_publication_date_that_was_not_retrieved_is_rejected():
    facts = sheet(
        Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead"),
        Fact("notice_published", "2026-07-23", "", "ENTSO-E A78"),
    )
    text = (
        "A notice published on 2026-07-19 was in force while the premium "
        "reached 109.84 EUR/MWh [ENTSO-E A44, ENTSO-E A78]."
    )
    assert not verify(text, facts).ok


def test_a_month_without_a_day_is_checked_at_month_precision():
    # "during August 2026" asserts less than a day does, and failing it for
    # being vague would be wrong. A different month is still wrong.
    ok = "The premium reached 109.84 EUR/MWh during August 2026 [ENTSO-E A44]."
    bad = "The premium reached 109.84 EUR/MWh during July 2026 [ENTSO-E A44]."
    assert verify(ok, standard()).ok
    assert not verify(bad, standard()).ok


def test_a_day_and_month_with_no_year_takes_the_episode_year():
    assert verify(
        "The split happened on 18 August with a premium of 109.84 EUR/MWh "
        "[ENTSO-E A44].",
        standard(),
    ).ok
    assert not verify(
        "The split happened on 19 August with a premium of 109.84 EUR/MWh "
        "[ENTSO-E A44].",
        standard(),
    ).ok


def test_an_episode_spanning_midnight_may_name_either_day():
    """The market day begins at local midnight, so the two differ honestly."""
    overnight = FactSheet(
        subject="overnight episode",
        start_utc=datetime(2026, 9, 3, 22, 15, tzinfo=timezone.utc),
        end_utc=datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc),
        market_day=date(2026, 9, 4),
        facts=[Fact("peak_premium", 109.84, "EUR/MWh", "ENTSO-E A44 day-ahead")],
    )
    for day in ("2026-09-03", "2026-09-04"):
        text = f"On {day} the premium reached 109.84 EUR/MWh [ENTSO-E A44]."
        assert verify(text, overnight).ok, day


def test_the_verdict_says_which_date_was_wrong():
    text = (
        "The market split on 2 September 2026 with a premium of 109.84 "
        "EUR/MWh [ENTSO-E A44]."
    )
    described = verify(text, standard()).describe()
    assert "2 September 2026" in described
    assert "Dates not in the evidence" in described


def test_digits_inside_a_retrieved_asset_name_are_not_claims():
    """`AT 2 400/220 SRM` is a transformer, not two measurements.

    The name came from the A78 notice and the agent was asked to quote it.
    Reading 400 and 220 as invented figures rejected an explanation that had
    done exactly the right thing.
    """
    facts = sheet(
        Fact("peak_premium", 16.25, "EUR/MWh", "ENTSO-E A44"),
        Fact("constrained_asset", "AT 2 400/220 SRM", "", "ENTSO-E A78"),
    )
    text = (
        "A planned outage on the AT 2 400/220 SRM asset was in force "
        "[ENTSO-E A78] while the premium reached 16.25 EUR/MWh [ENTSO-E A44]."
    )
    assert verify(text, facts).ok


def test_masking_a_name_does_not_licence_reusing_its_digits():
    """The narrow hole, pinned so it stays narrow."""
    facts = sheet(
        Fact("peak_premium", 16.25, "EUR/MWh", "ENTSO-E A44"),
        Fact("constrained_asset", "AT 2 400/220 SRM", "", "ENTSO-E A78"),
    )
    text = "The AT 2 400/220 SRM tripped and the border fell to 400 MW [A78]."
    verdict = verify(text, facts)

    assert not verdict.ok
    assert [claim.value for claim in verdict.unsupported] == [400.0]


def test_small_counting_numbers_do_not_fail_the_check():
    text = "The first of 2 constrained assets was named [ENTSO-E A78]."
    assert verify(text, standard()).ok


def test_an_explanation_with_no_source_is_not_grounded():
    """Every figure right and nothing traceable is still not evidence."""
    text = "Portugal paid 109.84 EUR/MWh more while capacity fell to 3195 MW."
    verdict = verify(text, standard())

    assert not verdict.ok
    assert verdict.missing_sources
    assert not verdict.unsupported


def test_source_matching_is_loose_enough_to_be_usable():
    """Citing A44 counts for "ENTSO-E A44 day-ahead"."""
    text = "The premium was 109.84 EUR/MWh, from A44."
    assert verify(text, standard()).ok


def test_prose_with_no_numbers_at_all_passes():
    assert verify("The zones priced apart for several hours.", standard()).ok


def test_extraction_reads_how_the_number_was_written():
    claims = extract_claims("109.84 EUR/MWh, 3,195 MW, and 37%")
    assert [claim.value for claim in claims] == [109.84, 3195.0, 37.0]
    assert [claim.decimals for claim in claims] == [2, 0, 0]
    assert [claim.is_percent for claim in claims] == [False, False, True]


def test_negative_numbers_are_checked_too():
    facts = sheet(Fact("premium", -28.22, "EUR/MWh", "ENTSO-E A44"))
    assert verify("The spread was -28.22 EUR/MWh [A44].", facts).ok
    assert not verify("The spread was -31.5 EUR/MWh [A44].", facts).ok


def test_a_typographic_minus_sign_is_still_a_negative_number():
    """Negative prices are normal here, and models type U+2212 for them."""
    facts = sheet(
        Fact("price_es_at_peak", -0.07, "EUR/MWh", "ENTSO-E A44"),
        Fact("price_pt_at_peak", -0.01, "EUR/MWh", "ENTSO-E A44"),
    )
    text = "Portugal traded at −0.01 EUR/MWh and Spain at −0.07 [A44]."
    assert verify(text, facts).ok


def test_an_em_dash_between_clauses_is_not_a_sign():
    text = (
        "The premium reached 109.84 EUR/MWh [ENTSO-E A44]—a severe "
        "separation—while capacity held at 3,195 MW."
    )
    assert verify(text, standard()).ok


def test_verdict_names_what_failed():
    text = "Capacity was 4200 MW and the premium 109.84 EUR/MWh [A44]."
    assert "4200" in verify(text, standard()).describe()


# --- the sheet itself -------------------------------------------------------


def build_episode(**overrides) -> pd.Series:
    row = {
        "start_utc": pd.Timestamp(START),
        "end_utc": pd.Timestamp(END),
        "market_day": date(2026, 8, 18),
        "duration_hours": 7.5,
        "intervals": 30,
        "peak_spread": 109.84,
        "premium_side": "PT",
        "max_severity": "severe",
        "extra_cost_eur": 1853272.0,
        "share_saturated": 1.0,
    }
    row.update(overrides)
    return pd.Series(row)


def build_intervals() -> pd.DataFrame:
    """Two intervals, deliberately unequal, so mixing them would be visible.

    The worst interval is the second: a 109.84 premium against the first's
    20.00. Facts drawn from a single interval must come from that one.
    """
    return pd.DataFrame(
        {
            "ts_utc": [pd.Timestamp(START), pd.Timestamp(START) + pd.Timedelta(minutes=15)],
            "price_pt_eur_mwh": [121.51, 163.00],
            "price_es_eur_mwh": [101.51, 53.16],
            "abs_premium_eur_mwh": [20.00, 109.84],
            "capacity_mw": [4000.0, 3195.0],
            "net_flow_mw": [3571.0, 3195.0],
        }
    )


def test_facts_from_one_interval_come_from_the_same_interval():
    """Mixing a maximum here with a minimum there produces impossible evidence.

    Taking the highest Portuguese price and the lowest Spanish price across the
    episode gives a difference that is not the spread, and the largest flow can
    exceed the smallest capacity, which reads as physically impossible. A model
    handed that would write something false, and the fault would be in the
    evidence rather than the model.
    """
    facts = episode_facts(build_episode(), build_intervals())

    price_pt = facts.get("price_pt_at_peak").value
    price_es = facts.get("price_es_at_peak").value
    capacity = facts.get("border_capacity_at_peak").value
    flow = facts.get("net_flow_es_to_pt_at_peak").value

    # All four come from the second interval, the worst one.
    assert (price_pt, price_es, capacity, flow) == (163.0, 53.16, 3195.0, 3195.0)
    # And they reconcile: the two prices differ by the peak premium.
    assert round(price_pt - price_es, 2) == facts.get("peak_premium").value
    # Flow cannot exceed capacity at the same moment.
    assert flow <= capacity


def test_the_sheet_only_allows_numbers_that_were_retrieved():
    facts = episode_facts(build_episode(), build_intervals())
    allowed = facts.numbers()

    assert 109.84 in allowed
    assert 3195.0 in allowed
    assert 1853272.0 in allowed
    # The lowest capacity across the episode is also retrieved, and labelled
    # as such rather than quietly mixed with the peak interval figures.
    assert facts.get("lowest_border_capacity").value == 3195.0
    # Never retrieved, and not derivable by the model either.
    assert 4200.0 not in allowed


def test_the_settlement_interval_length_is_retrieved_not_invented():
    """Every explanation wants to write "the single 15 minute interval"."""
    facts = episode_facts(
        build_episode(duration_hours=0.25, intervals=1), build_intervals()
    )
    minutes = facts.get("settlement_interval_minutes")

    assert minutes is not None and minutes.value == 15
    assert verify(
        "The split lasted a single 15 minute interval [ENTSO-E A44].", facts
    ).ok


def test_a_missing_notice_becomes_a_caveat_rather_than_silence():
    facts = episode_facts(build_episode(), build_intervals(), assets=None)

    assert facts.get("notices_in_force").value == 0
    assert any("unexplained" in caveat.lower() for caveat in facts.caveats)


def test_a_notice_that_does_not_account_for_the_drop_is_flagged():
    """The notice permits 4700 MW while the border had 3195."""
    assets = [
        {
            "asset": "Pereiros-Rio Maior 1",
            "available_mw": 4700.0,
            "status": "unplanned",
            "published_at": datetime(2026, 6, 25, tzinfo=timezone.utc),
        }
    ]
    facts = episode_facts(build_episode(), build_intervals(), assets=assets)

    assert any("does not account" in caveat for caveat in facts.caveats)


def test_saturation_carries_a_caveat_against_claiming_causation():
    facts = episode_facts(build_episode(), build_intervals())
    assert any("causation" in caveat for caveat in facts.caveats)


def test_the_rendered_sheet_carries_sources_for_the_model_to_cite():
    rendered = episode_facts(build_episode(), build_intervals()).render()

    assert "ENTSO-E A44 day-ahead" in rendered
    assert "ENTSO-E A61 day-ahead capacity" in rendered
    assert "Caveats" in rendered


def test_the_cost_fact_carries_the_note_that_stops_it_being_overstated():
    facts = episode_facts(build_episode(), build_intervals())
    cost = facts.get("extra_import_cost")

    assert cost is not None
    assert "not to national demand" in cost.note


def test_generated_text_is_checked_against_a_real_assembled_sheet():
    facts = episode_facts(build_episode(), build_intervals())

    honest = (
        "Portugal paid up to 109.84 EUR/MWh more than Spain [ENTSO-E A44 "
        "day-ahead] over 7.5 hours, while day-ahead capacity fell to 3,195 MW "
        "[ENTSO-E A61 day-ahead capacity]."
    )
    assert verify(honest, facts).ok

    invented = honest.replace("3,195 MW", "2,400 MW")
    assert not verify(invented, facts).ok