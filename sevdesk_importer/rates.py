"""USD-to-EUR rates: the ECB daily reference series, and the order of preference.

The ECB publishes D.USD.EUR.SP00.A quoted as **USD per EUR**. Every amount here is
USD, so the factor that converts is the *reciprocal* of the published quote. That
inversion is the single most dangerous line in this program: getting it backwards
is wrong by about 30% and still looks like money.

Rates resolve in one order — the rate recorded in the export, then the ECB quote for
the Buchungstag, then the most recent published business day before it, which is
always reported. If none resolves the row is not priced and the run refuses.
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sevdesk_importer.dates import today_in_vienna

ECB_SERIES = "D.USD.EUR.SP00.A"
ECB_ENDPOINT = "https://data-api.ecb.europa.eu/service/data/EXR"

#: How far back a booking date may reach for the previous published quote. Ten days
#: clears the longest TARGET closure (Christmas through New Year).
MAX_WALK_BACK_DAYS = 10

#: Extra history fetched before the window so the walk-back has somewhere to land.
WALK_BACK_HEADROOM = timedelta(days=MAX_WALK_BACK_DAYS)

CACHE_FILENAME = "ecb-usd-eur.json"
FETCH_TIMEOUT_SECONDS = 30


class RateUnavailable(Exception):
    """No rate could be established. The run refuses rather than guessing."""


def usd_to_eur_factor(quote: Decimal) -> Decimal:
    """Convert a published USD-per-EUR quote into the USD-to-EUR factor.

    1.1405 USD per EUR means one USD buys 0.87681 EUR. Full precision is kept;
    rounding happens once, at output.
    """
    return Decimal(1) / quote


def ecb_url(start: date, end: date) -> str:
    """The official SDMX endpoint for the whole span, queried in one request."""
    query = urllib.parse.urlencode(
        {
            "startPeriod": start.isoformat(),
            "endPeriod": end.isoformat(),
            "format": "csvdata",
        }
    )
    return f"{ECB_ENDPOINT}/{ECB_SERIES}?{query}"


def parse_ecb_csv(payload: str) -> dict[date, Decimal]:
    """Published quotes by date. Non-publication days are simply absent."""
    quotes: dict[date, Decimal] = {}
    for row in csv.DictReader(io.StringIO(payload)):
        period = (row.get("TIME_PERIOD") or "").strip()
        value = (row.get("OBS_VALUE") or "").strip()
        if not period or not value:
            continue
        quotes[date.fromisoformat(period)] = Decimal(value)
    return quotes


@dataclass(frozen=True)
class EcbSeries:
    """Published USD-per-EUR quotes, keyed by publication date."""

    quotes: Mapping[date, Decimal]

    def quote_on(self, day: date) -> Decimal | None:
        return self.quotes.get(day)


@dataclass(frozen=True)
class Rate:
    """The factor used to price one booking, and where it came from."""

    booking_date: date
    usd_to_eur: Decimal
    provenance: str
    ecb_quote: Decimal | None = None
    quote_date: date | None = None

    @property
    def identity(self) -> tuple[date, str, Decimal]:
        """What makes two rates the same rate, for reporting each one once."""
        return (self.booking_date, self.provenance, self.usd_to_eur)

    @property
    def is_substituted(self) -> bool:
        return self.provenance == "ecb_previous_business_day"

    @property
    def warning(self) -> str:
        if not self.is_substituted:
            return ""
        assert self.quote_date is not None and self.ecb_quote is not None
        return (
            f"No ECB reference rate published for {self.booking_date.isoformat()}; "
            f"used the previous published business day {self.quote_date.isoformat()} "
            f"({self.ecb_quote} USD/EUR)."
        )


def resolve(booking_date: date, recorded_rate: Decimal | None, series: EcbSeries) -> Rate:
    """Establish the USD-to-EUR factor for one Buchungstag, in order of preference."""
    if recorded_rate is not None:
        return Rate(booking_date, recorded_rate, "export")

    quote = series.quote_on(booking_date)
    if quote is not None:
        return Rate(booking_date, usd_to_eur_factor(quote), "ecb", quote, booking_date)

    for days_back in range(1, MAX_WALK_BACK_DAYS + 1):
        earlier = booking_date - timedelta(days=days_back)
        quote = series.quote_on(earlier)
        if quote is not None:
            return Rate(
                booking_date,
                usd_to_eur_factor(quote),
                "ecb_previous_business_day",
                quote,
                earlier,
            )

    raise RateUnavailable(
        f"No ECB reference rate for {booking_date.isoformat()} or any of the "
        f"{MAX_WALK_BACK_DAYS} days before it."
    )


def load_series(booking_dates: Sequence[date]) -> EcbSeries:
    """Quotes able to price every booking date, from the cache where possible.

    The cache is a performance optimisation only and is always safe to delete; the
    audit trail lives in the run report.
    """
    if not booking_dates:
        return EcbSeries({})

    cached = _read_cache()
    if cached is not None and _prices_every(cached, booking_dates):
        return EcbSeries(cached)

    start = min(booking_dates) - WALK_BACK_HEADROOM
    end = max(booking_dates)
    fetched = _fetch(start, end)
    if not fetched:
        raise RateUnavailable(
            f"The ECB returned no observations for {ECB_SERIES} between "
            f"{start.isoformat()} and {end.isoformat()}."
        )

    merged = dict(cached) if cached else {}
    merged.update(fetched)
    _write_cache(merged)
    return EcbSeries(merged)


def _prices_every(quotes: Mapping[date, Decimal], booking_dates: Sequence[date]) -> bool:
    """Whether cached quotes can already settle every booking date.

    A date the ECB never published resolves from an earlier business day, so a
    weekend settlement is a cache hit rather than a fetch. That substitution is only
    trusted once the date is in the past: until then its own quote may still appear,
    and using a stale neighbour would quietly outlive the publication that replaces it.
    """
    today = today_in_vienna()
    for day in booking_dates:
        if day in quotes:
            continue
        if day >= today:
            return False
        walk_back = (day - timedelta(days=n) for n in range(1, MAX_WALK_BACK_DAYS + 1))
        if not any(earlier in quotes for earlier in walk_back):
            return False
    return True


def _fetch(start: date, end: date) -> dict[date, Decimal]:
    url = ecb_url(start, end)
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as error:
        raise RateUnavailable(f"Could not reach the ECB at {url}: {error}") from error
    return parse_ecb_csv(payload)


def cache_path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "sevdesk-importer" / CACHE_FILENAME


def _read_cache() -> dict[date, Decimal] | None:
    try:
        stored = json.loads(cache_path().read_text(encoding="utf-8"))
        quotes = {
            date.fromisoformat(day): Decimal(value) for day, value in stored["quotes"].items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # A cache that cannot be read is rebuilt, never fatal.
        return None
    return quotes or None


def _write_cache(quotes: Mapping[date, Decimal]) -> None:
    payload = {
        "series": ECB_SERIES,
        "quoted": "USD per EUR",
        "quotes": {day.isoformat(): str(value) for day, value in sorted(quotes.items())},
    }
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        # Caching is best-effort; an unwritable cache must not fail the run.
        pass
