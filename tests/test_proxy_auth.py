import logging
import os
import runpy
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client

from reaper.models import User

PUBLIC_ORIGIN = "https://reporeaper.oddware.dev"
PUBLIC_HOST = "reporeaper.oddware.dev"
AZURE_HOST = "example.azurecontainerapps.io"
CALLBACK = PUBLIC_ORIGIN + "/auth/github/callback"


def production_config(**overrides):
    env = {
        "REAPER_DEBUG": "false",
        "SECRET_KEY": "test-only-" * 8,
        "TOKEN_ENCRYPTION_KEYS": "test-only-not-used-for-encryption",
        "DATABASE_URL": "postgresql://test:test@localhost/test",
        "APP_URL": f" {PUBLIC_ORIGIN}/ ",
        "ALLOWED_HOSTS": f" {PUBLIC_HOST}, {AZURE_HOST}, , ",
        "CSRF_TRUSTED_ORIGINS": f" {PUBLIC_ORIGIN}/, , ",
        "TRUST_PROXY": " true ",
        **overrides,
    }
    with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv"):
        return runpy.run_path(str(Path(__file__).resolve().parents[1] / "config/settings.py"))


def test_production_environment_parsing():
    config = production_config()
    assert config["APP_URL"] == PUBLIC_ORIGIN
    assert config["ALLOWED_HOSTS"] == [PUBLIC_HOST, AZURE_HOST]
    assert config["CSRF_TRUSTED_ORIGINS"] == [PUBLIC_ORIGIN]
    assert config["SECURE_PROXY_SSL_HEADER"] == ("HTTP_X_FORWARDED_PROTO", "https")
    assert config["USE_X_FORWARDED_HOST"] is False
    assert production_config(TRUST_PROXY="false")["SECURE_PROXY_SSL_HEADER"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"APP_URL": PUBLIC_ORIGIN + "/auth/github"},
        {"APP_URL": "https://user:password@reporeaper.oddware.dev"},
        {"APP_URL": PUBLIC_ORIGIN + "?query=1"},
        {"APP_URL": PUBLIC_ORIGIN + "#fragment"},
        {"APP_URL": "http://reporeaper.oddware.dev"},
        {"CSRF_TRUSTED_ORIGINS": PUBLIC_ORIGIN + "/auth/github"},
        {"CSRF_TRUSTED_ORIGINS": "https://*.oddware.dev"},
        {"ALLOWED_HOSTS": PUBLIC_ORIGIN},
        {"ALLOWED_HOSTS": PUBLIC_HOST + "/auth/github"},
        {"ALLOWED_HOSTS": PUBLIC_HOST + ":443"},
        {"ALLOWED_HOSTS": "*"},
        {"ALLOWED_HOSTS": " , "},
    ],
)
def test_invalid_production_origins_and_hosts_fail_at_startup(overrides):
    with pytest.raises(ImproperlyConfigured):
        production_config(**overrides)


@pytest.fixture
def proxy_client(settings):
    config = production_config()
    for name in (
        "DEBUG",
        "APP_URL",
        "ALLOWED_HOSTS",
        "CSRF_TRUSTED_ORIGINS",
        "SECURE_PROXY_SSL_HEADER",
        "USE_X_FORWARDED_HOST",
        "SECURE_SSL_REDIRECT",
        "SESSION_COOKIE_SECURE",
        "CSRF_COOKIE_SECURE",
        "SESSION_COOKIE_SAMESITE",
        "CSRF_COOKIE_SAMESITE",
        "SESSION_COOKIE_DOMAIN",
        "CSRF_COOKIE_DOMAIN",
    ):
        setattr(settings, name, config[name])
    return Client(
        enforce_csrf_checks=True,
        HTTP_HOST=PUBLIC_HOST,
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_FORWARDED_HOST="attacker.example",
    )


@pytest.mark.django_db
def test_proxy_oauth_round_trip_cookies_session_and_safe_logs(proxy_client, settings, caplog):
    logger = logging.getLogger("reaper.oauth")
    with patch.object(logger, "propagate", True), caplog.at_level(logging.INFO):
        response = proxy_client.get("/auth/github")
        assert response.status_code == 302
        assert response.wsgi_request.is_secure()
        assert response.wsgi_request.scheme == "https"
        assert response.wsgi_request.get_host() == PUBLIC_HOST
        assert response["Cache-Control"] == "no-store, private"
        query = parse_qs(urlsplit(response.url).query)
        assert urlsplit(response.url).netloc == "github.com"
        assert query["redirect_uri"] == [CALLBACK]
        pending = proxy_client.session["oauth"]
        old_session = proxy_client.session.session_key
        session_cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        assert session_cookie["secure"] and session_cookie["httponly"]
        assert session_cookie["samesite"] == "Lax"
        assert session_cookie["domain"] == ""
        with (
            patch(
                "reaper.views.token_exchange", return_value={"access_token": "ghu_secret-token"}
            ) as exchange,
            patch("reaper.views.GitHub.get", return_value={"id": 123, "login": "alice"}),
        ):
            response = proxy_client.get(
                "/auth/github/callback", {"state": pending["state"], "code": "secret-code"}
            )
        exchange.assert_called_once_with(
            {
                "code": "secret-code",
                "code_verifier": pending["verifier"],
                "redirect_uri": CALLBACK,
            }
        )
        assert response.url == "/dashboard"
        assert proxy_client.session["_auth_user_id"] == str(User.objects.get().pk)
        assert proxy_client.session.session_key != old_session
        assert "oauth" not in proxy_client.session
        csrf_cookie = response.cookies[settings.CSRF_COOKIE_NAME]
        assert csrf_cookie["secure"] and csrf_cookie["samesite"] == "Lax"
        assert csrf_cookie["domain"] == ""
        assert proxy_client.get("/dashboard").status_code == 200
        with patch("reaper.views.token_exchange") as exchange:
            assert (
                proxy_client.get(
                    "/auth/github/callback", {"state": pending["state"], "code": "secret-code"}
                ).url
                == "/login"
            )
            exchange.assert_not_called()
    assert CALLBACK in caplog.text
    assert '"is_secure": true' in caplog.text
    assert "HTTP_X_FORWARDED_HOST" in caplog.text
    for secret in (
        "secret-code",
        "ghu_secret-token",
        pending["state"],
        pending["verifier"],
        old_session,
        csrf_cookie.value,
        settings.GITHUB_CLIENT_SECRET,
    ):
        assert secret not in caplog.text


@pytest.mark.django_db
def test_callback_origin_is_canonical_even_with_azure_host(proxy_client):
    response = proxy_client.get("/auth/github", HTTP_HOST=AZURE_HOST)
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.url).query)["redirect_uri"] == [CALLBACK]


@pytest.mark.django_db
def test_production_csrf_origin_and_referer_validation(proxy_client, users, settings):
    proxy_client.force_login(users[0])
    proxy_client.get("/dashboard")
    token = proxy_client.cookies[settings.CSRF_COOKIE_NAME].value
    for headers in (
        {"HTTP_ORIGIN": PUBLIC_ORIGIN},
        {"HTTP_REFERER": PUBLIC_ORIGIN + "/settings"},
    ):
        assert (
            proxy_client.post("/auth/github", {"csrfmiddlewaretoken": token}, **headers).status_code
            == 302
        )
    assert proxy_client.post("/auth/github", HTTP_ORIGIN=PUBLIC_ORIGIN).status_code == 403
    assert (
        proxy_client.post(
            "/auth/github", {"csrfmiddlewaretoken": token}, HTTP_ORIGIN="https://attacker.example"
        ).status_code
        == 403
    )
    assert (
        proxy_client.post(
            "/logout", {"csrfmiddlewaretoken": token}, HTTP_ORIGIN=PUBLIC_ORIGIN
        ).status_code
        == 302
    )


@pytest.mark.django_db
def test_original_azure_origin_mismatch_and_corrected_trust(proxy_client, users, settings):
    proxy_client.force_login(users[0])
    proxy_client.get("/dashboard")
    token = proxy_client.cookies[settings.CSRF_COOKIE_NAME].value
    settings.CSRF_TRUSTED_ORIGINS = ["https://" + AZURE_HOST]
    headers = {"HTTP_HOST": AZURE_HOST, "HTTP_ORIGIN": PUBLIC_ORIGIN}
    assert (
        proxy_client.post("/auth/github", {"csrfmiddlewaretoken": token}, **headers).status_code
        == 403
    )
    settings.CSRF_TRUSTED_ORIGINS = [PUBLIC_ORIGIN]
    # Reload middleware, whose trusted origins are cached per instance.
    proxy_client.handler._middleware_chain = None
    assert (
        proxy_client.post("/auth/github", {"csrfmiddlewaretoken": token}, **headers).status_code
        == 302
    )


@pytest.mark.django_db
def test_untrusted_host_rejected_and_http_redirects_to_https(proxy_client):
    assert proxy_client.get("/auth/github", HTTP_HOST="attacker.example").status_code == 400
    response = proxy_client.get("/auth/github", HTTP_X_FORWARDED_PROTO="http")
    assert response.status_code == 301
    assert response.url == PUBLIC_ORIGIN + "/auth/github"
