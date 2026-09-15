"""ENTSO-E Transparency Platform client.

Design note: this client returns the RAW XML payload rather than a parsed
dataframe. Bronze stores exactly what the API returned, so the raw response is
replayable and parsing bugs can be fixed without re-hitting the API. Parsing
lives in iberian.parsing and runs on the bronze -> silver hop.

API reference: https://web-api.tp.entsoe.eu/api
Token: passed as the securityToken query parameter.
Time format: YYYYMMDDHHMM, always UTC.
"""

from __future__ import annotations

import gzip
import io
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from iberian.config import (
    DOC_DAY_AHEAD_PRICES,
    DOC_FORECASTED_CAPACITY,
    DOC_GENERATION_UNAVAILABILITY,
    DOC_PHYSICAL_FLOWS,
    DOC_SCHEDULED_EXCHANGES,
    DOC_TRANSMISSION_UNAVAILABILITY,
    ENTSOE_BASE_URL,
)

ENTSOE_TIME_FORMAT = "%Y%m%d%H%M"


class EntsoeRequestError(RuntimeError):
    """A 4xx from ENTSO-E, with the reason text pulled out of the body.

    ENTSO-E puts the actual explanation in an Acknowledgement document rather
    than the status line, so the useful message is in the body, not the code.
    """

    def __init__(self, status_code: int, params: dict[str, str], body: str) -> None:
        self.status_code = status_code
        self.params = params
        self.body = body
        self.reason = self._extract_reason(body)

        hint = ""
        if status_code in (401, 403):
            hint = (
                " Check ENTSOE_SECURITY_TOKEN: it is the Web API token from "
                "My Account, and it is only shown once when generated."
            )

        super().__init__(
            f"ENTSO-E returned {status_code} for {params}. "
            f"Reason: {self.reason or '(none given)'}.{hint}"
        )

    @staticmethod
    def _extract_reason(body: str) -> str | None:
        import re

        match = re.search(r"<text>(.*?)</text>", body, re.DOTALL)
        return match.group(1).strip() if match else None


@dataclass(frozen=True)
class RawResponse:
    """A raw ENTSO-E payload plus the request context needed for bronze.

    The payload is kept as bytes because ENTSO-E does not always answer with
    XML. Large responses, and unavailability documents in particular, come back
    as a ZIP archive holding many XML files. Decoding those to text corrupts
    them, and the failure surfaces as a confusing parse error at line 1 rather
    than anything that names the real problem.
    """

    params: dict[str, str]
    content: bytes
    fetched_at_utc: str
    status_code: int

    @property
    def is_zip(self) -> bool:
        return self.content[:4] == b"PK\x03\x04"

    @property
    def is_gzip(self) -> bool:
        return self.content[:2] == b"\x1f\x8b"

    @property
    def body(self) -> str:
        """The single XML document, for responses that carry exactly one."""
        if self.is_zip:
            raise ValueError(
                "This response is a ZIP archive holding several documents. "
                "Use .documents() instead of .body."
            )
        return self.content.decode("utf-8")

    def documents(self) -> list[str]:
        """Every XML document in the response, whether or not it was zipped."""
        if self.is_zip:
            with zipfile.ZipFile(io.BytesIO(self.content)) as archive:
                return [
                    archive.read(name).decode("utf-8")
                    for name in sorted(archive.namelist())
                    if not name.endswith("/")
                ]
        if self.is_gzip:
            return [gzip.decompress(self.content).decode("utf-8")]

        text = self.content.decode("utf-8", errors="replace").lstrip("﻿").lstrip()
        if not text.startswith("<"):
            preview = self.content[:32]
            raise ValueError(
                f"Response is neither XML nor a ZIP archive. First bytes: {preview!r}"
            )
        return [text]

    @property
    def suggested_extension(self) -> str:
        return ".zip" if self.is_zip else ".xml"

    @property
    def is_empty(self) -> bool:
        """ENTSO-E answers 'no data' with an Acknowledgement document, not a 404."""
        if self.is_zip or self.is_gzip:
            return False
        return b"Acknowledgement_MarketDocument" in self.content


class EntsoeClient:
    def __init__(
        self,
        security_token: str,
        base_url: str = ENTSOE_BASE_URL,
        timeout_seconds: int = 60,
        max_retries: int = 3,
    ) -> None:
        self._token = security_token
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._session = requests.Session()

    def _get(self, params: dict[str, str]) -> RawResponse:
        query = {**params, "securityToken": self._token}
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                response = self._session.get(
                    self._base_url, params=query, timeout=self._timeout
                )

                # 429 means the rate limit kicked in. Back off rather than hammer.
                if response.status_code == 429:
                    time.sleep(2**attempt)
                    continue

                # Any other 4xx is our fault, not a blip. Retrying a bad token
                # or a malformed parameter three times just delays the real
                # message, so fail immediately and show what ENTSO-E said.
                if 400 <= response.status_code < 500:
                    raise EntsoeRequestError(
                        status_code=response.status_code,
                        params=params,
                        body=response.text,
                    )

                response.raise_for_status()
                return RawResponse(
                    params=params,
                    content=response.content,
                    fetched_at_utc=datetime.now(timezone.utc).isoformat(),
                    status_code=response.status_code,
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2**attempt)

        raise RuntimeError(
            f"ENTSO-E request failed after {self._max_retries} attempts: {last_error}"
        )

    @staticmethod
    def _fmt(dt: datetime) -> str:
        if dt.tzinfo is None:
            raise ValueError("Pass timezone-aware datetimes, ENTSO-E expects UTC.")
        return dt.astimezone(timezone.utc).strftime(ENTSOE_TIME_FORMAT)

    def day_ahead_prices(
        self, eic_code: str, period_start: datetime, period_end: datetime
    ) -> RawResponse:
        """Day-ahead prices for one bidding zone.

        For a price document, in_Domain and out_Domain are both the bidding zone.
        """
        return self._get(
            {
                "documentType": DOC_DAY_AHEAD_PRICES,
                "in_Domain": eic_code,
                "out_Domain": eic_code,
                "periodStart": self._fmt(period_start),
                "periodEnd": self._fmt(period_end),
            }
        )

    def physical_flows(
        self,
        out_domain: str,
        in_domain: str,
        period_start: datetime,
        period_end: datetime,
    ) -> RawResponse:
        """Cross-border physical flows, directional. Call once per direction."""
        return self._get(
            {
                "documentType": DOC_PHYSICAL_FLOWS,
                "out_Domain": out_domain,
                "in_Domain": in_domain,
                "periodStart": self._fmt(period_start),
                "periodEnd": self._fmt(period_end),
            }
        )

    def forecasted_transfer_capacity(
        self,
        out_domain: str,
        in_domain: str,
        period_start: datetime,
        period_end: datetime,
    ) -> RawResponse:
        """Day-ahead forecasted transfer capacity, the NTC [11.1.A].

        This is the denominator of the saturation test: when the scheduled
        exchange reaches this number, the interconnection is full and the two
        zones are free to price apart.

        Confirmed against the platform's API user guide: documentType A61 with
        contract_MarketAgreement.type A01 for day-ahead. The platform returns a
        single direction per request, so call it twice with the domains
        swapped.
        """
        return self._get(
            {
                "documentType": DOC_FORECASTED_CAPACITY,
                "contract_MarketAgreement.type": "A01",
                "out_Domain": out_domain,
                "in_Domain": in_domain,
                "periodStart": self._fmt(period_start),
                "periodEnd": self._fmt(period_end),
            }
        )

    def scheduled_exchanges(
        self,
        out_domain: str,
        in_domain: str,
        period_start: datetime,
        period_end: datetime,
        contract_type: str = "A01",
    ) -> RawResponse:
        """Scheduled commercial exchanges [12.1.F], the numerator.

        Less certain than A61: the API user guide section I could read does not
        spell this one out, so treat A09 as a candidate until the probe script
        confirms it returns data for the PT/ES border.
        """
        return self._get(
            {
                "documentType": DOC_SCHEDULED_EXCHANGES,
                "contract_MarketAgreement.type": contract_type,
                "out_Domain": out_domain,
                "in_Domain": in_domain,
                "periodStart": self._fmt(period_start),
                "periodEnd": self._fmt(period_end),
            }
        )

    def generation_unavailability(
        self, eic_code: str, period_start: datetime, period_end: datetime
    ) -> RawResponse:
        """A80 generation outage notices, keyed on a bidding zone.

        These explain what is offline inside a zone. They do NOT explain
        interconnection capacity, which is a transmission matter: see
        transmission_unavailability.
        """
        return self._get(
            {
                "documentType": DOC_GENERATION_UNAVAILABILITY,
                "biddingZone_Domain": eic_code,
                "periodStart": self._fmt(period_start),
                "periodEnd": self._fmt(period_end),
            }
        )

    def transmission_unavailability(
        self,
        out_domain: str,
        in_domain: str,
        period_start: datetime,
        period_end: datetime,
        business_type: str | None = None,
        published_start: datetime | None = None,
        published_end: datetime | None = None,
    ) -> RawResponse:
        """A78 transmission infrastructure unavailability, keyed on the border.

        This is what explains a collapse in interconnection capacity. Confirmed
        against the API user guide: documentType A78 with in_Domain and
        out_Domain, businessType B12 for unplanned and B13 for planned, and
        both returned when businessType is omitted.

        published_start and published_end map to periodStartUpdate and
        periodEndUpdate, which filter by when the notice was published rather
        than when the outage happens. That distinction is the whole point in
        time correctness story: to evaluate cause attribution honestly, only
        notices published before an anomaly may be retrieved for it, and the
        platform can enforce that server side.
        """
        params = {
            "documentType": DOC_TRANSMISSION_UNAVAILABILITY,
            "out_Domain": out_domain,
            "in_Domain": in_domain,
            "periodStart": self._fmt(period_start),
            "periodEnd": self._fmt(period_end),
        }
        if business_type:
            params["businessType"] = business_type
        if published_start:
            params["periodStartUpdate"] = self._fmt(published_start)
        if published_end:
            params["periodEndUpdate"] = self._fmt(published_end)
        return self._get(params)
