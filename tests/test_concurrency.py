from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections

from reaper.models import Repository, RepositoryScan
from reaper.services.jobs import claim_job, enqueue
from reaper.services.security import ServiceError

pytestmark = pytest.mark.django_db(transaction=True)


def parallel(function):
    barrier = Barrier(2)

    def task():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return function()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(task) for _ in range(2)]
        return [future.result(timeout=20) for future in futures]


def test_concurrent_enqueue_is_idempotent(users):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL concurrency semantics")
    repo = Repository.objects.create(user=users[0], github_id=301, name="concurrent", owner="alice")
    ids = parallel(lambda: enqueue(users[0], repo).pk)
    assert ids[0] == ids[1]
    assert RepositoryScan.objects.count() == 1
    users[0].refresh_from_db()
    assert users[0].scan_budget_used == 1


def test_workers_claim_distinct_jobs(users):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL skip-locked semantics")
    for i in range(2):
        repo = Repository.objects.create(
            user=users[0], github_id=400 + i, name=f"worker-{i}", owner="alice"
        )
        enqueue(users[0], repo)
    claimed = parallel(claim_job)
    assert claimed[0].pk != claimed[1].pk
    assert all(scan.status == "fetching" for scan in claimed)


def test_deleting_analyses_does_not_reset_cost_budget(users, settings):
    settings.DAILY_SCAN_LIMIT = 1
    repo = Repository.objects.create(user=users[0], github_id=501, name="cost", owner="alice")
    enqueue(users[0], repo)
    repo.scans.all().delete()
    with pytest.raises(ServiceError, match="Daily"):
        enqueue(users[0], repo)
