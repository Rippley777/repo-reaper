import os

os.environ.setdefault("REAPER_DEBUG", "true")
os.environ.setdefault("REAPER_ENV", "test")

import pytest
from cryptography.fernet import Fernet
from django.utils import timezone

from reaper.models import (
    AIAccount,
    GitHubAccount,
    Repository,
    RepositoryAnalysis,
    RepositoryScan,
    User,
)
from reaper.services.analysis import AnalysisResult
from reaper.services.security import encrypt


@pytest.fixture(autouse=True)
def test_settings(settings):
    settings.DEBUG = True
    settings.SECURE_SSL_REDIRECT = False
    settings.SESSION_COOKIE_SECURE = False
    settings.CSRF_COOKIE_SECURE = False
    settings.TOKEN_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.GITHUB_CLIENT_ID = "test-client"
    settings.GITHUB_CLIENT_SECRET = "test-secret"
    settings.AI_MODELS = ["gpt-4.1-mini"]


@pytest.fixture
def users(db):
    a = User.objects.create_user(
        username="github_1", github_user_id=1, github_username="alice", ai_consent=True
    )
    b = User.objects.create_user(
        username="github_2", github_user_id=2, github_username="bob", ai_consent=True
    )
    for user in (a, b):
        GitHubAccount.objects.create(user=user, access_token=encrypt("ghu_" + "x" * 30))
        AIAccount.objects.create(
            user=user,
            api_key=encrypt(f"sk-test-{user.github_user_id}-" + "x" * 30),
            key_hint="••••xxxx",
        )
    return a, b


@pytest.fixture
def result():
    return AnalysisResult(
        summary="A small distributed task queue.",
        original_purpose="Learn durable task processing.",
        current_condition="Prototype with tests, no verified deployment.",
        project_status="Functional Prototype",
        resurrection_effort="Small",
        effort_explanation="Add integration tests and deploy a worker.",
        portfolio_value="High",
        portfolio_angle="Demonstrates backend concurrency and reliability.",
        complexity="Moderate",
        architecture="Database-backed worker",
        technical_debt=["Runtime version requires verification."],
        security_concerns=[],
        deployment_readiness="Unverified",
        reusable_code=["Queue implementation"],
        resurrection_plan=["Add a worker failure recovery test."],
        suggested_next_step="Add a worker failure recovery test.",
        detected_languages=["Python"],
        detected_frameworks=["Django"],
        confidence="Medium",
        uncertainty=["Production deployment has not been verified."],
        evidence=["metadata"],
    )


@pytest.fixture
def repos(users, result):
    repos = []
    for user in users:
        repo = Repository.objects.create(
            user=user,
            github_id=100 + user.github_user_id,
            owner=user.github_username,
            name=f"{user.github_username}-private-project",
            description=f"{user.github_username} confidential",
            private=True,
        )
        scan = RepositoryScan.objects.create(
            user=user,
            repository=repo,
            status="complete",
            available_at=timezone.now(),
            completed_at=timezone.now(),
            analysis_version="1.0",
            model="gpt-4.1-mini",
            commit_sha="abc",
        )
        RepositoryAnalysis.objects.create(
            user=user,
            scan=scan,
            result=result.model_dump(),
            project_status=result.project_status,
            resurrection_effort=result.resurrection_effort,
            portfolio_value=result.portfolio_value,
        )
        repos.append(repo)
    return repos


@pytest.fixture
def github_metadata():
    return {
        "id": 101,
        "name": "renamed-project",
        "full_name": "alice/renamed-project",
        "owner": {"login": "alice"},
        "private": False,
        "archived": False,
        "fork": False,
        "language": "Python",
        "default_branch": "main",
        "description": "A queue",
        "stargazers_count": 3,
        "pushed_at": "2026-01-01T00:00:00Z",
        "topics": ["backend"],
    }
