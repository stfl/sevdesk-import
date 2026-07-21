"""Shared fixtures.

No test touches the network. The ECB call is patched at the HTTP boundary, so URL
construction and response parsing stay under test, using a captured real response.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    """Stands in for what urllib.request.urlopen returns."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture
def ecb_response() -> bytes:
    """A real ECB SDMX csvdata response for D.USD.EUR.SP00.A, captured verbatim."""
    return (FIXTURES / "ecb-usd-eur.csv").read_bytes()


@pytest.fixture
def wise_statement() -> Path:
    return FIXTURES / "wise-usd.csv"


@pytest.fixture
def revolut_statement() -> Path:
    return FIXTURES / "revolut-usd.csv"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any test that forgets to patch the ECB call fails loudly instead of dialling out."""

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("tests must not reach the network; patch urllib.request.urlopen")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


@pytest.fixture
def offline_ecb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ecb_response: bytes, no_network: None
) -> None:
    """Serve the captured ECB response, with the rate cache pointed somewhere disposable."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def fake_urlopen(url: str, *_: object, **__: object) -> FakeResponse:
        return FakeResponse(ecb_response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
