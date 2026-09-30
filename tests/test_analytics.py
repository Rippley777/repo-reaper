import pytest

from reaper.analytics import browser_analytics


@pytest.fixture
def analytics(settings):
    settings.HOUSE_EDGE_ENABLED = True
    settings.HOUSE_EDGE_PROJECT = "repo-reaper"
    settings.HOUSE_EDGE_KEY = "he_pk_test_browser_key"
    settings.HOUSE_EDGE_ENDPOINT = "https://analytics.example.com/api/collect"
    return settings


def test_analytics_requires_explicit_configuration(analytics):
    analytics.HOUSE_EDGE_ENABLED = False
    assert browser_analytics() is None
    analytics.HOUSE_EDGE_ENABLED = True
    analytics.HOUSE_EDGE_KEY = ""
    assert browser_analytics() is None


@pytest.mark.parametrize("endpoint", ["invalid", "javascript:alert(1)", "https://user:pass@example.com"])
def test_invalid_analytics_does_not_break_the_host(analytics, endpoint):
    analytics.HOUSE_EDGE_ENDPOINT = endpoint
    assert browser_analytics() is None


@pytest.mark.django_db
def test_template_and_csp_use_the_configured_collector(client, analytics):
    response = client.get("/")
    assert response.status_code == 200
    assert b'data-project="repo-reaper"' in response.content
    assert b'data-endpoint="https://analytics.example.com/api/collect"' in response.content
    assert "connect-src 'self' https://analytics.example.com;" in response["Content-Security-Policy"]
    assert "script-src 'self';" in response["Content-Security-Policy"]
    analytics.HOUSE_EDGE_ENABLED = False
    response = client.get("/")
    assert b"house-edge-0.1.1.js" not in response.content
    assert "connect-src 'self';" in response["Content-Security-Policy"]
