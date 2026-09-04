"""tests/test_corp_events.py — hermetic Form-4/8-K event tests."""

from __future__ import annotations

import pytest

import corp_events
import edgar
from tests.fixtures_edgar import FORM4_XML, submissions


@pytest.fixture(autouse=True)
def _no_cache_leak():
    corp_events.reset_cache()
    yield
    corp_events.reset_cache()


@pytest.fixture
def http(tmp_path, monkeypatch):
    routes: dict[str, bytes] = {}
    routes["company_tickers.json"] = (
        b'[{"cik_str":872589,"ticker":"REGN","title":"Regeneron"}]')

    def fake_get(url: str) -> bytes:
        for key, payload in routes.items():
            if key in url:
                return payload
        raise edgar.EdgarError(f"no route for {url}")

    monkeypatch.setattr(edgar, "_http_get", fake_get)
    monkeypatch.setenv("EDGAR_CACHE_DIR", str(tmp_path / "cache"))
    edgar.clear_cache()
    return routes


class TestForm4Parse:
    def test_parse_real_shape_xml(self):
        trades = corp_events.parse_form4(FORM4_XML)
        assert len(trades) == 2
        sale = trades[0]
        assert sale["owner"] == "Guarini Kathryn"
        assert sale["role"] == "Director"
        assert sale["code"] == "S"
        assert sale["shares"] == 400
        assert sale["price"] == 850.0
        assert sale["date"] == "2026-09-02"
        exercise = trades[1]
        assert exercise["code"] == "M"
        assert exercise["price"] == 719.37

    def test_parse_garbage_raises(self):
        with pytest.raises(edgar.EdgarError):
            corp_events.parse_form4("<html>not a form4</html>")


class TestEventsBlock:
    def test_form4_and_8k_surface(self, http):
        routes = http
        routes["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["4", "4", "8-K"]))
        routes["000166375826000002/edgardoc.xml"] = FORM4_XML.encode()
        block = corp_events.events_block("REGN", since="2026-08-25")
        assert "Form 4" in block
        assert "Guarini Kathryn" in block
        assert "SOLD 400 @ $850.00" in block
        assert "8-K filed 2026-09-02" in block

    def test_events_block_empty(self, http):
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["10-Q"]))
        assert corp_events.events_block("REGN", since="2026-08-25") == ""

    def test_no_events_since_window(self, http):
        # submission dates are all before the window start
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["4", "10-Q"]))
        assert corp_events.events_block("REGN", since="2026-09-10") == ""

    def test_failure_returns_empty(self, http):
        assert corp_events.events_block("REGN", since="2026-08-25") == ""

    def test_bad_form4_xml_skipped_not_fatal(self, http):
        routes = http
        routes["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["4"]))
        routes["000166375826000002/edgardoc.xml"] = b"<html>oops</html>"
        # parse failure -> filing skipped, other content still rendered
        assert corp_events.events_block("REGN", since="2026-08-25") == ""

    def test_collapses_cashless_exercise_into_sale(self):
        """M 400 @ 719.37 + S 400 @ 850 same owner/date = one cashless line."""
        trades = corp_events.parse_form4(FORM4_XML)
        line = corp_events._render_trades(trades)
        assert "EXERCISED 400 options @ $719.37" in line
        assert "SOLD 400 @ $850.00 ($340,000)" in line
        assert line.count("SOLD") == 1  # one combined cashless line
