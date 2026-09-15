"""Shared configuration and domain constants.

Everything here is environment agnostic: no Databricks imports, no secrets in
code. Secrets come from environment variables so the same modules run in a
local VS Code session, in a Databricks notebook, and in a Lakeflow pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ENTSO-E EIC area codes.
# Verify these against the first successful API response: a wrong EIC returns
# a "No matching data found" acknowledgement rather than a hard error, so it
# can look like an empty result instead of a bad parameter.
EIC_PORTUGAL = "10YPT-REN------W"
EIC_SPAIN = "10YES-REE------0"

BIDDING_ZONES = {
    "PT": EIC_PORTUGAL,
    "ES": EIC_SPAIN,
}

# ENTSO-E document types used by this project.
DOC_DAY_AHEAD_PRICES = "A44"       # Price document, confirmed
DOC_FORECASTED_CAPACITY = "A61"    # Day-ahead NTC [11.1.A], confirmed in the API guide
DOC_PHYSICAL_FLOWS = "A11"         # Aggregated energy data report, candidate
DOC_SCHEDULED_EXCHANGES = "A09"    # Finalised schedule [12.1.F], candidate
DOC_GENERATION_UNAVAILABILITY = "A80"
# Transmission infrastructure unavailability [10.1.A and 10.1.B], confirmed.
# This is the one that explains interconnection capacity, and it is keyed on the
# border with in_Domain and out_Domain, not on a bidding zone.
DOC_TRANSMISSION_UNAVAILABILITY = "A78"
DOC_PRODUCTION_UNAVAILABILITY = "A77"  # [15.1.A-D], keyed on biddingZone_Domain

# businessType narrows an unavailability query. Omitting it returns both.
BUSINESS_TRANSMISSION_UNPLANNED = "B12"
BUSINESS_TRANSMISSION_PLANNED = "B13"
BUSINESS_PRODUCTION_UNPLANNED = "B14"
BUSINESS_PRODUCTION_PLANNED = "B15"
DOC_OFFERED_CAPACITY = "A31"       # Offered capacity, fallback if A61 is thin
DOC_TOTAL_NOMINATED = "A26"        # Total capacity nominated, fallback

ENTSOE_BASE_URL = "https://web-api.tp.entsoe.eu/api"

# contract_MarketAgreement.type separates the auctions inside one A44 document.
# Without filtering on this you get day-ahead and every intraday session stacked
# on the same timestamps, which is not a parser bug, it is several markets.
CONTRACT_TYPE_LABELS = {
    "A01": "day_ahead",   # Daily
    "A07": "intraday",    # Intraday
    "A13": "hourly",
}
DEFAULT_MARKETS = frozenset({"day_ahead", "unknown"})

# The Iberian market day runs from local midnight in CET, which is 22:00Z in
# summer and 23:00Z in winter, not UTC midnight. Ask for a UTC calendar day and
# ENTSO-E hands back two market days, partially overlapping.
# Portugal keeps WET/WEST on the clock, but MIBEL publishes both zones on the
# Spanish market day boundary, which is what the A44 documents show.
MARKET_TIMEZONE = "Europe/Madrid"

# Market splitting thresholds, in EUR/MWh.
# Under normal coupling PT and ES clear at exactly the same price, so anything
# above a rounding epsilon is a real decoupling, not noise.
DECOUPLING_EPSILON = 0.01
SEVERITY_BANDS = [
    (0.01, 5.0, "minor"),
    (5.0, 20.0, "moderate"),
    (20.0, float("inf"), "severe"),
]


@dataclass(frozen=True)
class Settings:
    entsoe_token: str | None
    raw_data_dir: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            entsoe_token=os.environ.get("ENTSOE_SECURITY_TOKEN"),
            raw_data_dir=os.environ.get("RAW_DATA_DIR", "data/raw"),
        )

    def require_entsoe_token(self) -> str:
        if not self.entsoe_token:
            raise RuntimeError(
                "ENTSOE_SECURITY_TOKEN is not set. Copy .env.example to .env "
                "and fill it in, or export the variable in your shell."
            )
        return self.entsoe_token
