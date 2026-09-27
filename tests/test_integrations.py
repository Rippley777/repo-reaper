import base64
import json
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.utils import timezone

from reaper.models import GitHubAccount, GitHubCache, Repository, User
from reaper.services.analysis import OpenAIProvider
from reaper.services.evidence import collect, redact
from reaper.services.github import GitHub, user_token
from reaper.services.security import ServiceError, decrypt, encrypt

pytestmark = pytest.mark.django_db


@contextmanager
def response_stream(status=200, data=None, headers=None):
    yield httpx.Response(
        status,
        json=data,
        headers=headers,
        request=httpx.Request("GET", "https://api.github.com/test"),
    )


def test_user_scoped_cache_etag_and_request_deduplication(users):
    with patch(
        "reaper.services.github.httpx.stream",
        side_effect=lambda *a, **k: response_stream(
            data={"private": "alice"}, headers={"etag": '"one"'}
        ),
    ) as stream:
        github = GitHub(users[0].pk)
        assert github.get("/test") == {"private": "alice"}
        github.get("/test")
        GitHub(users[0].pk).get("/test")
        assert stream.call_count == 1
        GitHub(users[1].pk).get("/test")
        assert stream.call_count == 2
    cache = GitHubCache.objects.get(user=users[0])
    assert "alice" not in cache.body
    cache.expires_at = timezone.now() - timedelta(seconds=1)
    cache.save()
    with patch(
        "reaper.services.github.httpx.stream",
        side_effect=lambda *a, **k: response_stream(status=304),
    ) as stream:
        assert GitHub(users[0].pk).get("/test") == {"private": "alice"}
        assert stream.call_args.kwargs["headers"]["If-None-Match"] == '"one"'


def test_late_response_cannot_restore_deleted_cache(users):
    github = GitHub(users[0].pk)
    User.objects.filter(pk=users[0].pk).update(privacy_epoch=1)
    with patch(
        "reaper.services.github.httpx.stream",
        side_effect=lambda *a, **k: response_stream(data={"private": "sensitive"}),
    ):
        github.get("/test")
    assert not GitHubCache.objects.exists()


@pytest.mark.parametrize(
    "status,headers,expected",
    [
        (401, {}, "revoked"),
        (404, {}, "unavailable"),
        (403, {"x-ratelimit-remaining": "0", "retry-after": "90"}, "rate limit"),
        (500, {}, "temporarily"),
    ],
)
def test_github_failures_are_safe(users, status, headers, expected):
    with (
        patch(
            "reaper.services.github.httpx.stream",
            side_effect=lambda *a, **k: response_stream(
                status=status, data={"message": "sensitive provider internals"}, headers=headers
            ),
        ),
        pytest.raises(ServiceError, match=expected) as err,
    ):
        GitHub(users[0].pk).get("/test")
    assert "sensitive" not in str(err.value)


def test_no_arbitrary_hosts_or_redirects(users):
    github = GitHub(users[0].pk)
    with pytest.raises(ServiceError):
        github.get("https://attacker.test/token")
    with (
        patch(
            "reaper.services.github.httpx.stream",
            side_effect=lambda *a, **k: response_stream(
                status=302, headers={"location": "https://attacker.test"}
            ),
        ) as stream,
        pytest.raises(ServiceError),
    ):
        github.get("/test")
    assert stream.call_args.kwargs["follow_redirects"] is False


def test_refresh_rotates_both_tokens(users):
    account = GitHubAccount.objects.get(user=users[0])
    account.expires_at = timezone.now() - timedelta(seconds=1)
    account.refresh_token = encrypt("ghr_old")
    account.save()
    with patch(
        "reaper.services.github.token_exchange",
        return_value={
            "access_token": "ghu_new",
            "refresh_token": "ghr_new",
            "expires_in": 28800,
            "refresh_token_expires_in": 100000,
        },
    ):
        assert user_token(users[0].pk) == "ghu_new"
    account.refresh_from_db()
    assert decrypt(account.refresh_token) == "ghr_new"
    assert account.expires_at > timezone.now()


def test_evidence_samples_text_handles_bad_manifests_and_skips_secrets(github_metadata):
    github = MagicMock()
    entries = [
        {"path": name, "type": "blob", "mode": "100644", "size": 200, "sha": str(i)}
        for i, name in enumerate(
            ["README.md", "package.json", ".env", "node_modules/x/package.json", "src/main.py"]
        )
    ]

    def get(path):
        if "/git/trees/" in path:
            return {"tree": entries, "truncated": True}
        if "/git/blobs/" in path:
            return {
                "encoding": "base64",
                "content": base64.b64encode(
                    b'{invalid json; api_key="sk-123456789012345678901234567890"'
                ).decode(),
            }
        if "/languages" in path:
            return {"Python": 1000}
        return []

    github.get.side_effect = get
    evidence = collect(github, github_metadata, "abc")
    assert "package.json" in evidence["files"]
    assert ".env" not in evidence["files"]
    assert "node_modules/x/package.json" not in evidence["files"]
    assert "sk-123456" not in json.dumps(evidence)
    assert any("truncated" in warning for warning in evidence["limitations"])


def test_empty_repository_has_explicit_uncertainty(github_metadata):
    github = MagicMock()
    github.get.return_value = {}
    evidence = collect(github, github_metadata, "")
    assert evidence["files"] == {}
    assert any("Empty repository" in warning for warning in evidence["limitations"])


def test_redacts_common_tokens_and_private_keys():
    value = (
        "ghp_"
        + "a" * 30
        + '\npassword="hello"\n-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----'
    )
    cleaned = redact(value)
    assert "hello" not in cleaned and "ghp_" not in cleaned and "secret" not in cleaned


def test_structured_provider_disables_storage_validates_evidence_and_tracks_usage(settings, result):
    api_key = "sk-test-" + "x" * 30
    body = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": result.model_dump_json()}],
            }
        ],
        "usage": {"input_tokens": 15, "output_tokens": 20},
    }
    response = httpx.Response(
        200, json=body, request=httpx.Request("POST", "https://api.openai.com/v1/responses")
    )
    with patch("reaper.services.analysis.httpx.post", return_value=response) as post:
        analysis, inputs, outputs = OpenAIProvider(api_key).analyze(
            {"evidence_index": ["metadata"]}, "gpt-4.1-mini"
        )
        assert analysis.portfolio_value == "High"
        assert (inputs, outputs) == (15, 20)
        payload = post.call_args.kwargs["json"]
        assert post.call_args.kwargs["headers"]["Authorization"] == f"Bearer {api_key}"
        assert payload["store"] is False and "tools" not in payload
        assert payload["text"]["format"]["strict"] is True
        with pytest.raises(ServiceError, match="unsupported evidence"):
            OpenAIProvider(api_key).analyze({"evidence_index": []}, "gpt-4.1-mini")


def test_provider_rejects_bad_user_key_without_exposing_upstream_body(settings):
    response = httpx.Response(
        401,
        text="sensitive upstream response",
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    with patch("reaper.services.analysis.httpx.post", return_value=response):
        with pytest.raises(ServiceError, match="rejected your API key") as exc:
            OpenAIProvider("sk-test-" + "x" * 30).analyze({"evidence_index": []}, "gpt-4.1-mini")
    assert "sensitive upstream response" not in str(exc.value)


def test_import_creates_scans_only_from_owned_discovery(client, users, github_metadata):
    client.force_login(users[0])
    GitHubCache.objects.create(
        user=users[0],
        key="discovery",
        body=encrypt(json.dumps([github_metadata])),
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    response = client.post("/repositories/import", {"repositories": [101], "user_id": users[1].pk})
    assert response.url == "/dashboard"
    repo = Repository.objects.get(github_id=101)
    assert repo.user == users[0]
    assert repo.scans.get().status == "queued"


def test_discovery_and_settings_routes(client, users, github_metadata):
    client.force_login(users[0])
    with patch("reaper.views.GitHub.discover", return_value=[github_metadata]):
        response = client.get("/repositories")
    assert response.status_code == 200
    assert b"renamed-project" in response.content
    client.post(
        "/settings/save",
        {
            "model": "gpt-4.1-mini",
            "ai_consent": "on",
            "allow_private": "on",
            "user_id": users[1].pk,
        },
    )
    users[0].refresh_from_db()
    users[1].refresh_from_db()
    assert users[0].allow_private and not users[1].allow_private


def test_evidence_hard_budget_even_with_huge_path_inventory(settings):
    from reaper.services.evidence import finalize

    settings.MAX_EVIDENCE_CHARS = 2000
    evidence = {
        "metadata": {},
        "files": {},
        "tree": ["long/" * 100 + str(i) for i in range(200)],
        "evidence_index": ["metadata", "tree"],
        "limitations": [],
    }
    bounded = finalize(evidence)
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 2000
    assert bounded["limitations"]


def test_empty_repo_metadata_is_redacted(github_metadata):
    github = MagicMock()
    github.get.return_value = {}
    github_metadata["description"] = "ghp_" + "z" * 30
    assert "ghp_" not in json.dumps(collect(github, github_metadata, ""))
