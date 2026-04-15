import pytest
import responses as rsps_lib
from adapters.company_ir import CompanyIRAdapter

APPLE_IR_HTML = """
<html><body>
<div class="press-release-item">
  <a href="/press-releases/2024-10-31-q4-results">Apple Reports Fourth Quarter 2024 Results</a>
  <span class="date">October 31, 2024</span>
</div>
<div class="press-release-item">
  <a href="/press-releases/2024-09-09-iphone16">Apple Introduces iPhone 16 Family</a>
  <span class="date">September 9, 2024</span>
</div>
</body></html>
"""

APPLE_EVENTS_HTML = """
<html><body>
<div class="event-item">
  <span class="event-title">Q4 2024 Earnings Call</span>
  <span class="event-date">October 31, 2024</span>
</div>
</body></html>
"""

GENERIC_HTML = """
<html><body>
<article>
  <h2><a href="/news/2024-q3-results">Q3 2024 Results</a></h2>
  <time datetime="2024-08-01">August 1, 2024</time>
</article>
</body></html>
"""


@rsps_lib.activate
def test_test_access_ok(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://investor.apple.com/press-releases/default.aspx",
                 body=APPLE_IR_HTML, status=200, content_type="text/html")
    adapter = CompanyIRAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "ok"


@rsps_lib.activate
def test_test_access_error_on_403(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://investor.apple.com/press-releases/default.aspx",
                 status=403)
    adapter = CompanyIRAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    result, err = adapter.test_access()
    assert result == "error"
    assert "403" in err


@rsps_lib.activate
def test_fetch_press_releases_returns_dict(minimal_settings, sample_tickers):
    rsps_lib.add(rsps_lib.GET,
                 "https://investor.apple.com/press-releases/default.aspx",
                 body=APPLE_IR_HTML, status=200, content_type="text/html")
    rsps_lib.add(rsps_lib.GET,
                 "https://nvidianews.nvidia.com/releases",
                 body=GENERIC_HTML, status=200, content_type="text/html")
    adapter = CompanyIRAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    tickers_with_urls = [t for t in sample_tickers if t.get("ir_press_url")]
    data = adapter.fetch_sample_data(tickers_with_urls[:2], "press_releases")
    assert "AAPL" in data
    assert data["AAPL"]["status"] in ("ok", "parsed", "no_items_found", "error")


def test_validate_schema_ok(minimal_settings, sample_tickers):
    data = {
        "AAPL": {"status": "ok", "items": [
            {"title": "Q4 Results", "date": "2024-10-31", "url": "/pr/q4"}
        ]},
    }
    adapter = CompanyIRAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "press_releases")
    assert ok is True


def test_validate_schema_missing_title(minimal_settings, sample_tickers):
    data = {
        "AAPL": {"status": "ok", "items": [{"date": "2024-10-31"}]},
    }
    adapter = CompanyIRAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    ok, missing = adapter.validate_schema(data, "press_releases")
    assert ok is False
    assert "items[].title" in missing


def test_evaluate_freshness_recent(minimal_settings, sample_tickers):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=2)).isoformat()
    data = {"AAPL": {"status": "ok", "items": [{"title": "Test", "date": recent}]}}
    adapter = CompanyIRAdapter(settings=minimal_settings, tickers_config=sample_tickers)
    freshness = adapter.evaluate_freshness(data, "press_releases")
    assert freshness in ("1d", "same-day", "1w")
