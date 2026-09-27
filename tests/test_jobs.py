from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from reaper.models import AIAccount, GitHubAccount, Repository, RepositoryAnalysis, RepositoryScan
from reaper.services.jobs import claim_job, enqueue, run_job
from reaper.services.security import ServiceError

pytestmark = pytest.mark.django_db


@pytest.fixture
def job(users):
    repo = Repository.objects.create(user=users[0], github_id=101, owner="alice", name="project")
    return enqueue(users[0], repo)


def test_enqueue_deduplicates_and_enforces_daily_quota(users, job, settings):
    assert enqueue(users[0], job.repository).pk == job.pk
    settings.DAILY_SCAN_LIMIT = 1
    another = Repository.objects.create(user=users[0], github_id=200, name="another", owner="alice")
    with pytest.raises(ServiceError, match="Daily"):
        enqueue(users[0], another)


def test_consent_and_private_opt_in_are_required(users, repos):
    with pytest.raises(ServiceError, match="private"):
        enqueue(users[0], repos[0])
    users[0].ai_consent = False
    users[0].save()
    with pytest.raises(ServiceError, match="consent"):
        enqueue(users[0], repos[0])


def test_user_api_key_is_required(users):
    AIAccount.objects.filter(user=users[0]).delete()
    repo = Repository.objects.create(user=users[0], github_id=222, owner="alice", name="keyless")
    with pytest.raises(ServiceError, match="OpenAI API key"):
        enqueue(users[0], repo)


def mocked_scan(scan, metadata, result, side_effect=None):
    provider = MagicMock()
    provider.analyze.return_value = (result, 123, 456)
    if side_effect:
        provider.analyze.side_effect = side_effect
    evidence = {
        "metadata": {"description": "Queue"},
        "files": {"README.md": "sample source"},
        "evidence_index": ["metadata"],
        "limitations": [],
    }
    with (
        patch("reaper.services.jobs.GitHub") as github,
        patch("reaper.services.jobs.collect", return_value=evidence),
    ):
        github.return_value.repository.return_value = metadata
        github.return_value.commit.return_value = {"sha": "abcdef"}
        run_job(scan, provider)
    return provider


def test_scan_lifecycle_persistence_and_same_repo_cache(users, job, result, github_metadata):
    scan = claim_job()
    assert scan.pk == job.pk and scan.status == "fetching"
    assert claim_job() is None
    provider = mocked_scan(scan, github_metadata, result)
    provider.analyze.assert_called_once()
    scan.refresh_from_db()
    assert scan.status == "complete"
    assert scan.analysis.user_id == users[0].pk
    assert scan.input_tokens == 123
    assert "files" not in scan.evidence
    scan.repository.refresh_from_db()
    assert scan.repository.name == "renamed-project"
    enqueue(users[0], scan.repository)
    second = claim_job()
    provider = mocked_scan(second, github_metadata, result)
    provider.analyze.assert_not_called()
    second.refresh_from_db()
    assert second.cached and second.input_tokens == 0
    assert RepositoryAnalysis.objects.count() == 2
    assert second.changes == ["No material change in the audit conclusions."]


def test_user_cache_not_shared(users, job, result, github_metadata):
    mocked_scan(claim_job(), github_metadata, result)
    other = Repository.objects.create(
        user=users[1], github_id=101, owner="alice", name="shared-repo"
    )
    enqueue(users[1], other)
    provider = mocked_scan(claim_job(), github_metadata, result)
    provider.analyze.assert_called_once()
    assert RepositoryAnalysis.objects.filter(user=users[1]).count() == 1


def test_provider_failure_does_not_fail_other_jobs(users, job, result, github_metadata):
    second_repo = Repository.objects.create(
        user=users[0], github_id=102, owner="alice", name="other"
    )
    second = enqueue(users[0], second_repo)
    mocked_scan(claim_job(), github_metadata, result, ServiceError("AI unavailable"))
    job.refresh_from_db()
    second.refresh_from_db()
    assert job.status == "failed" and second.status == "queued"
    assert not RepositoryAnalysis.objects.exists()


def test_deleted_job_cannot_be_resurrected(users, job, result, github_metadata):
    scan = claim_job()

    def delete_while_analyzing(*args):
        Repository.objects.filter(pk=scan.repository_id).delete()
        return result, 1, 1

    mocked_scan(scan, github_metadata, result, delete_while_analyzing)
    assert not RepositoryAnalysis.objects.exists()
    assert not RepositoryScan.objects.exists()


def test_disconnected_worker_cannot_persist(users, job, result, github_metadata):
    def disconnect(*args):
        GitHubAccount.objects.filter(user=users[0]).delete()
        return result, 1, 1

    mocked_scan(claim_job(), github_metadata, result, disconnect)
    assert not RepositoryAnalysis.objects.exists()


def test_removed_ai_key_worker_cannot_persist(users, job, result, github_metadata):
    def remove_key(*args):
        AIAccount.objects.filter(user=users[0]).delete()
        return result, 1, 1

    mocked_scan(claim_job(), github_metadata, result, remove_key)
    assert not RepositoryAnalysis.objects.exists()


def test_revoked_private_access_prevents_ai(users, job, result, github_metadata):
    github_metadata["private"] = True
    provider = mocked_scan(claim_job(), github_metadata, result)
    provider.analyze.assert_not_called()
    job.refresh_from_db()
    assert job.status == "failed"


def test_lease_expiration_fences_old_worker(job):
    first = claim_job()
    RepositoryScan.objects.filter(pk=first.pk).update(
        lease_until=timezone.now() - timedelta(seconds=1)
    )
    second = claim_job()
    assert second.pk == first.pk
    assert second.lease_token != first.lease_token
    from reaper.services.jobs import LostLease, touch

    with pytest.raises(LostLease):
        touch(first)


def test_expired_ai_job_is_not_automatically_rebilled(job):
    scan = claim_job()
    RepositoryScan.objects.filter(pk=scan.pk).update(
        status="analyzing", lease_until=timezone.now() - timedelta(seconds=1)
    )
    assert claim_job() is None
    scan.refresh_from_db()
    assert scan.status == "failed"
    assert "duplicate charges" in scan.error


def test_rate_limit_reschedules_without_busy_wait(job):
    scan = claim_job()
    with patch(
        "reaper.services.jobs.GitHub", side_effect=ServiceError("Rate limited", retry_after=120)
    ):
        run_job(scan)
    scan.refresh_from_db()
    assert scan.status == "queued"
    assert scan.available_at > timezone.now() + timedelta(seconds=110)
