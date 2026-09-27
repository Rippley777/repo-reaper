import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.utils import timezone

from reaper.models import (
    AIAccount,
    GitHubAccount,
    GitHubCache,
    Repository,
    RepositoryAnalysis,
    RepositoryScan,
    ResurrectionQueueItem,
)
from reaper.services.jobs import enqueue
from reaper.services.security import decrypt, encrypt

pytestmark = pytest.mark.django_db


def test_private_pages_require_login(client, repos):
    for path in [
        "/dashboard",
        "/repositories",
        "/graveyard",
        "/queue",
        "/settings",
        f"/repos/{repos[0].pk}",
        f"/api/scans/{repos[0].scans.first().pk}",
    ]:
        response = client.get(path)
        assert response.status_code == 302
        assert response.url.startswith("/login")


@pytest.mark.parametrize(
    "method,suffix", [("get", ""), ("post", "/rescan"), ("post", "/delete"), ("post", "/queue")]
)
def test_user_a_cannot_access_user_b_repository(client, users, repos, method, suffix):
    client.force_login(users[0])
    response = getattr(client, method)(f"/repos/{repos[1].pk}{suffix}")
    assert response.status_code == 404
    assert Repository.objects.filter(pk=repos[1].pk).exists()
    assert repos[1].scans.count() == 1
    assert not ResurrectionQueueItem.objects.exists()


def test_foreign_scan_is_not_visible(client, users, repos):
    client.force_login(users[0])
    foreign_scan = repos[1].scans.first()
    assert client.get(f"/api/scans/{foreign_scan.pk}").status_code == 404
    assert client.get(f"/repos/{repos[0].pk}?scan={foreign_scan.pk}").status_code == 404


@pytest.mark.parametrize("path", ["/dashboard", "/graveyard", "/queue", "/settings"])
def test_rendered_pages_never_contain_other_user_data(client, users, repos, path):
    for user, repo in zip(users, repos, strict=True):
        ResurrectionQueueItem.objects.create(user=user, repository=repo)
    client.force_login(users[0])
    response = client.get(path)
    assert response.status_code == 200
    assert b"bob-private-project" not in response.content
    assert b"bob confidential" not in response.content


def test_csrf_protects_mutations(users, repos):
    client = Client(enforce_csrf_checks=True)
    client.force_login(users[0])
    for path in [
        f"/repos/{repos[0].pk}/delete",
        "/repositories/import",
        "/settings/save",
        "/settings/ai-key",
        "/settings/ai-key/delete",
        "/settings/privacy",
        "/auth/github",
        "/logout",
    ]:
        assert client.post(path).status_code == 403


def test_mutations_reject_get(client, users, repos):
    client.force_login(users[0])
    for path in [
        f"/repos/{repos[0].pk}/delete",
        f"/repos/{repos[0].pk}/rescan",
        "/settings/save",
        "/settings/ai-key",
        "/settings/ai-key/delete",
        "/settings/privacy",
        "/auth/github",
        "/logout",
    ]:
        assert client.get(path).status_code == 405


def test_forged_import_is_rejected(client, users, repos, github_metadata):
    client.force_login(users[0])
    GitHubCache.objects.create(
        user=users[1],
        key="discovery",
        body=encrypt(json.dumps([github_metadata])),
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    assert client.post("/repositories/import", {"repositories": [101]}).status_code == 403
    assert repos[0].scans.count() == 1


def test_enqueue_rejects_foreign_repository(users, repos):
    with pytest.raises(Repository.DoesNotExist):
        enqueue(users[0], repos[1])


def test_delete_only_owned_data(client, users, repos):
    client.force_login(users[0])
    client.post(f"/repos/{repos[0].pk}/delete")
    assert not Repository.objects.filter(pk=repos[0].pk).exists()
    assert RepositoryAnalysis.objects.filter(user=users[1]).count() == 1


def test_delete_all_scoped_and_confirmation_required(client, users, repos):
    client.force_login(users[0])
    client.post("/settings/privacy", {"action": "delete_all", "confirmation": "wrong"})
    assert Repository.objects.count() == 2
    client.post("/settings/privacy", {"action": "delete_all", "confirmation": "DELETE ANALYSES"})
    assert not Repository.objects.filter(user=users[0]).exists()
    assert Repository.objects.filter(user=users[1]).exists()


def test_encryption_is_randomized_and_round_trips():
    a, b = encrypt("sensitive-token"), encrypt("sensitive-token")
    assert a != b and "sensitive-token" not in a
    assert decrypt(a) == "sensitive-token"


def test_ai_key_is_encrypted_and_user_scoped(client, users):
    client.force_login(users[0])
    raw_key = "sk-project-" + "a" * 32
    response = client.post("/settings/ai-key", {"api_key": raw_key, "user_id": users[1].pk})
    assert response.status_code == 302
    account = AIAccount.objects.get(user=users[0])
    other = AIAccount.objects.get(user=users[1])
    assert raw_key not in account.api_key
    assert decrypt(account.api_key) == raw_key
    assert decrypt(other.api_key) != raw_key
    page = client.get("/settings")
    assert raw_key.encode() not in page.content
    assert account.api_key.encode() not in page.content
    client.post("/settings/ai-key/delete", {"user_id": users[1].pk})
    assert not AIAccount.objects.filter(user=users[0]).exists()
    assert AIAccount.objects.filter(user=users[1]).exists()


def test_postgres_composite_constraints(users, repos, result):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-specific tenant foreign keys")
    with pytest.raises(IntegrityError), transaction.atomic():
        RepositoryScan.objects.create(
            user=users[0],
            repository=repos[1],
            available_at=timezone.now(),
            analysis_version="1",
            model="x",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        ResurrectionQueueItem.objects.create(user=users[0], repository=repos[1])
    scan = RepositoryScan.objects.create(
        user=users[1],
        repository=repos[1],
        available_at=timezone.now(),
        analysis_version="1",
        model="x",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        RepositoryAnalysis.objects.create(
            user=users[0],
            scan=scan,
            result=result.model_dump(),
            project_status="Unknown",
            resurrection_effort="Small",
            portfolio_value="Low",
        )


def test_private_cache_headers_and_xss_escaping(client, users, repos):
    repos[0].description = '<script>alert("xss")</script>'
    repos[0].save()
    client.force_login(users[0])
    response = client.get("/dashboard")
    assert response["Cache-Control"] == "no-store, private"
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"]
    assert b"<script>alert(" not in response.content
    assert b"&lt;script&gt;" in response.content


def test_disconnect_removes_credentials_even_when_revocation_fails(client, users, repos):
    client.force_login(users[0])
    with patch("reaper.views.revoke", return_value=False):
        response = client.post(
            "/settings/privacy", {"action": "disconnect", "confirmation": "DISCONNECT"}, follow=True
        )
    assert response.status_code == 200
    assert not GitHubAccount.objects.filter(user=users[0]).exists()
    assert Repository.objects.filter(user=users[0]).exists()
    assert b"revocation could not be confirmed" in response.content
