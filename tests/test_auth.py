import time
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest

from reaper.models import GitHubAccount, User
from reaper.services.security import decrypt

pytestmark = pytest.mark.django_db


def start(client):
    response = client.get("/auth/github")
    assert response.status_code == 302
    return parse_qs(urlparse(response.url).query)


def test_oauth_state_pkce_no_write_scopes(client):
    query = start(client)
    assert len(query["state"][0]) > 30
    assert query["code_challenge_method"] == ["S256"]
    assert "scope" not in query
    assert client.session["oauth"]["verifier"] != query["code_challenge"][0]


@pytest.mark.parametrize("mode", ["wrong", "expired", "denied", "missing"])
def test_bad_callback_never_exchanges_tokens(client, mode):
    query = start(client)
    params = {"state": query["state"][0], "code": "code"}
    if mode == "wrong":
        params["state"] = "bad"
    if mode == "expired":
        session = client.session
        pending = session["oauth"]
        pending["issued"] = time.time() - 700
        session["oauth"] = pending
        session.save()
    if mode == "denied":
        params["error"] = "access_denied"
    if mode == "missing":
        params.pop("code")
    with patch("reaper.views.token_exchange") as exchange:
        assert client.get("/auth/github/callback", params).status_code == 302
        exchange.assert_not_called()
    assert User.objects.count() == 0
    assert "oauth" not in client.session


def test_login_uses_stable_numeric_id_encrypts_tokens_and_prevents_replay(client):
    profile = {
        "id": 123,
        "login": "original",
        "name": "Example",
        "avatar_url": "https://avatars.githubusercontent.com/u/123",
    }
    token = "ghu_" + "a" * 30
    for username in ("original", "renamed"):
        profile["login"] = username
        query = start(client)
        old_session = client.session.session_key
        with (
            patch("reaper.views.token_exchange", return_value={"access_token": token}),
            patch("reaper.views.GitHub.get", return_value=profile),
        ):
            response = client.get(
                "/auth/github/callback", {"state": query["state"][0], "code": "code"}
            )
        assert response.url == "/dashboard"
        if username == "original":
            assert client.session.session_key != old_session
    assert User.objects.count() == 1
    user = User.objects.get(github_user_id=123)
    assert user.github_username == "renamed"
    assert not user.has_usable_password()
    assert decrypt(GitHubAccount.objects.get(user=user).access_token) == token
    with patch("reaper.views.token_exchange") as exchange:
        client.get("/auth/github/callback", {"state": query["state"][0], "code": "code"})
        exchange.assert_not_called()
    client.post("/logout")
    assert "_auth_user_id" not in client.session


def test_oauth_rate_limit(client):
    for _ in range(10):
        client.get("/auth/github")
    assert client.get("/auth/github").status_code == 429
