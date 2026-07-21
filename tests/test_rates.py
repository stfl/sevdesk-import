"""ECB reference rates: fetching, inversion, resolution order and caching.

The ECB series D.USD.EUR.SP00.A is quoted USD per EUR. The USD-to-EUR factor is
its reciprocal, and getting that backwards produces plausible-looking numbers
wrong by about 30%.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import FakeResponse

from sevdesk_importer.rates import (
    ECB_SERIES,
    EcbSeries,
    RateUnavailable,
    ecb_url,
    load_series,
    parse_ecb_csv,
    resolve,
    usd_to_eur_factor,
)


class RecordingOpener:
    """Stands in for urllib.request.urlopen at the HTTP boundary."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, url: str, *_: object, **__: object) -> FakeResponse:
        self.urls.append(url)
        return FakeResponse(self.payload)


@pytest.fixture
def opener(
    monkeypatch: pytest.MonkeyPatch, ecb_response: bytes, no_network: None
) -> RecordingOpener:
    recorder = RecordingOpener(ecb_response)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    return recorder


@pytest.fixture
def cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


class TestInversion:
    def test_reciprocal_of_a_known_published_quote(self) -> None:
        """1.1405 USD per EUR means one USD is worth 0.87681 EUR."""
        factor = usd_to_eur_factor(Decimal("1.1405"))
        assert factor.quantize(Decimal("0.00001")) == Decimal("0.87681")

    def test_a_dollar_is_worth_less_than_a_euro_at_these_quotes(self) -> None:
        """The cheap guard against using the quote uninverted."""
        assert usd_to_eur_factor(Decimal("1.1537")) < 1

    def test_full_precision_is_retained(self) -> None:
        factor = usd_to_eur_factor(Decimal("1.1537"))
        assert factor * Decimal("1.1537") == pytest.approx(Decimal(1))
        assert len(factor.as_tuple().digits) > 6


class TestParsing:
    def test_url_targets_the_official_series_and_range(self) -> None:
        url = ecb_url(date(2026, 5, 1), date(2026, 6, 15))
        assert ECB_SERIES == "D.USD.EUR.SP00.A"
        assert f"/EXR/{ECB_SERIES}" in url
        assert "startPeriod=2026-05-01" in url
        assert "endPeriod=2026-06-15" in url

    def test_parses_publication_dates_and_quotes(self, ecb_response: bytes) -> None:
        quotes = parse_ecb_csv(ecb_response.decode())
        assert quotes[date(2026, 6, 11)] == Decimal("1.1537")
        assert quotes[date(2026, 4, 20)] == Decimal("1.176")

    def test_non_publication_days_are_simply_absent(self, ecb_response: bytes) -> None:
        quotes = parse_ecb_csv(ecb_response.decode())
        assert date(2026, 5, 2) not in quotes, "Saturday"
        assert date(2026, 5, 3) not in quotes, "Sunday"
        assert date(2026, 5, 1) not in quotes, "Labour Day, a TARGET holiday"


class TestResolutionOrder:
    @pytest.fixture
    def series(self, ecb_response: bytes) -> EcbSeries:
        return EcbSeries(parse_ecb_csv(ecb_response.decode()))

    def test_a_rate_recorded_in_the_export_wins(self, series: EcbSeries) -> None:
        rate = resolve(date(2026, 6, 11), Decimal("0.876543"), series)
        assert rate.usd_to_eur == Decimal("0.876543")
        assert rate.provenance == "export"

    def test_otherwise_the_ecb_quote_for_the_booking_date(self, series: EcbSeries) -> None:
        rate = resolve(date(2026, 6, 11), None, series)
        assert rate.provenance == "ecb"
        assert rate.quote_date == date(2026, 6, 11)
        assert rate.ecb_quote == Decimal("1.1537")
        assert rate.usd_to_eur == usd_to_eur_factor(Decimal("1.1537"))

    def test_a_sunday_falls_back_to_the_previous_published_day(self, series: EcbSeries) -> None:
        """2026-05-03 is a Sunday; 05-02 a Saturday; 05-01 Labour Day."""
        rate = resolve(date(2026, 5, 3), None, series)
        assert rate.provenance == "ecb_previous_business_day"
        assert rate.quote_date == date(2026, 4, 30)
        assert rate.ecb_quote == Decimal("1.1702")

    def test_the_fallback_is_reported_never_silent(self, series: EcbSeries) -> None:
        rate = resolve(date(2026, 5, 3), None, series)
        assert rate.is_substituted
        assert "2026-04-30" in rate.warning
        assert "2026-05-03" in rate.warning

    def test_an_exact_hit_is_not_flagged_as_substituted(self, series: EcbSeries) -> None:
        assert not resolve(date(2026, 6, 11), None, series).is_substituted

    def test_it_refuses_rather_than_guessing_when_nothing_resolves(self, series: EcbSeries) -> None:
        with pytest.raises(RateUnavailable) as excinfo:
            resolve(date(2030, 1, 15), None, series)
        assert "2030-01-15" in str(excinfo.value)

    def test_it_never_reaches_forward_to_a_later_quote(self, series: EcbSeries) -> None:
        """A date before the series begins must refuse, not borrow a future rate."""
        with pytest.raises(RateUnavailable):
            resolve(date(2026, 4, 19), None, series)


BUSINESS_DAYS = [date(2026, 5, 4), date(2026, 6, 11)]


class TestFetching:
    def test_the_series_is_fetched_once_for_the_whole_span(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        series = load_series(BUSINESS_DAYS)
        assert len(opener.urls) == 1
        assert series.quote_on(date(2026, 6, 11)) == Decimal("1.1537")

    def test_nothing_to_price_asks_the_ecb_nothing(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        assert load_series([]).quotes == {}
        assert opener.urls == []

    def test_the_span_is_widened_to_allow_walking_back(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        load_series(BUSINESS_DAYS)
        assert "startPeriod=2026-05-04" not in opener.urls[0]

    def test_a_second_run_over_known_dates_makes_no_request(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        load_series(BUSINESS_DAYS)
        load_series(BUSINESS_DAYS)
        assert len(opener.urls) == 1

    def test_a_past_non_publication_day_is_served_from_the_cache(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        """2026-05-31 is a Sunday. Non-publication days are common, not an edge case,
        so a statement ending on one must not re-fetch on every run."""
        load_series([date(2026, 5, 31)])
        load_series([date(2026, 5, 31)])
        assert len(opener.urls) == 1

    def test_a_date_that_may_still_be_published_is_always_refetched(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        """Until a date is in the past, its own quote may yet replace a substitution."""
        future = date(2099, 6, 1)
        load_series([future])
        load_series([future])
        assert len(opener.urls) == 2

    def test_the_cache_stores_published_quotes_not_inverted_factors(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        """The spec places a JSON object keyed by ISO date under XDG_CACHE_HOME, so
        the figures stay citable to their publisher rather than derived."""
        load_series(BUSINESS_DAYS)
        (cached,) = list((cache_dir / "sevdesk-importer").glob("*.json"))
        stored = json.loads(cached.read_text())
        assert stored["quotes"]["2026-06-11"] == "1.1537"

    def test_an_unknown_date_triggers_a_refetch(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        load_series(BUSINESS_DAYS)
        load_series([*BUSINESS_DAYS, date(2026, 12, 30)])
        assert len(opener.urls) == 2

    def test_deleting_the_cache_is_always_safe(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        first = load_series(BUSINESS_DAYS)
        for path in (cache_dir / "sevdesk-importer").glob("*.json"):
            path.unlink()
        second = load_series(BUSINESS_DAYS)
        assert first.quote_on(date(2026, 6, 11)) == second.quote_on(date(2026, 6, 11))

    def test_a_corrupt_cache_is_rebuilt_rather_than_fatal(
        self, opener: RecordingOpener, cache_dir: Path
    ) -> None:
        cache_home = cache_dir / "sevdesk-importer"
        cache_home.mkdir(parents=True, exist_ok=True)
        (cache_home / "ecb-usd-eur.json").write_text("{ not json")
        series = load_series(BUSINESS_DAYS)
        assert series.quote_on(date(2026, 6, 11)) == Decimal("1.1537")
