import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q


class User(AbstractUser):
    github_user_id = models.BigIntegerField(unique=True, null=True, blank=True)
    github_username = models.CharField(max_length=100, blank=True)
    display_name = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(blank=True)
    allow_private = models.BooleanField(default=False)
    ai_consent = models.BooleanField(default=False)
    privacy_epoch = models.PositiveIntegerField(default=0)
    scan_budget_started_at = models.DateTimeField(null=True)
    scan_budget_used = models.PositiveIntegerField(default=0)
    ai_model = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def profile_url(self):
        return f"https://github.com/{self.github_username}"


class GitHubAccount(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True)
    refresh_expires_at = models.DateTimeField(null=True)
    scopes = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class AIAccount(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    api_key = models.TextField()
    key_hint = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Repository(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    github_id = models.BigIntegerField()
    owner = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    private = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    fork = models.BooleanField(default=False)
    language = models.CharField(max_length=100, blank=True)
    default_branch = models.CharField(max_length=255, default="main")
    stars = models.PositiveIntegerField(default=0)
    pushed_at = models.DateTimeField(null=True)
    latest_commit_sha = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "github_id"], name="repo_user_github_unique"),
            models.UniqueConstraint(fields=["id", "user"], name="repo_id_user_unique"),
        ]
        indexes = [models.Index(fields=["user", "name"])]

    @property
    def full_name(self):
        return f"{self.owner}/{self.name}"

    @property
    def github_url(self):
        return f"https://github.com/{self.owner}/{self.name}"


ACTIVE_STATES = ["queued", "fetching", "inspecting", "analyzing"]


class RepositoryScan(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        FETCHING = "fetching", "Fetching Repository"
        INSPECTING = "inspecting", "Inspecting Files"
        ANALYZING = "analyzing", "Analyzing"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="scans")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    commit_sha = models.CharField(max_length=64, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    analysis_version = models.CharField(max_length=40)
    model = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    available_at = models.DateTimeField()
    lease_until = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    cached = models.BooleanField(default=False)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    evidence = models.JSONField(default=dict)
    changes = models.JSONField(default=list)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "repository"],
                condition=Q(status__in=ACTIVE_STATES),
                name="one_active_scan_per_repo",
            ),
            models.UniqueConstraint(fields=["id", "user"], name="scan_id_user_unique"),
        ]
        indexes = [
            models.Index(fields=["status", "available_at"]),
            models.Index(fields=["user", "repository", "created_at"]),
        ]


class RepositoryAnalysis(models.Model):
    scan = models.OneToOneField(
        RepositoryScan, on_delete=models.CASCADE, primary_key=True, related_name="analysis"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    project_status = models.CharField(max_length=40, db_index=True)
    resurrection_effort = models.CharField(max_length=30, db_index=True)
    portfolio_value = models.CharField(max_length=10, db_index=True)
    result = models.JSONField()


class ResurrectionQueueItem(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="queue_items")
    priority = models.CharField(
        max_length=10,
        choices=[("Next", "Next"), ("Soon", "Soon"), ("Someday", "Someday")],
        default="Soon",
    )
    notes = models.CharField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "repository"], name="queue_user_repo_unique")
        ]


class GitHubCache(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    key = models.CharField(max_length=64)
    body = models.TextField()  # Encrypted; may contain private repository metadata.
    etag = models.CharField(max_length=255, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="cache_user_key_unique")
        ]
        indexes = [models.Index(fields=["expires_at"])]


class RateBucket(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    window = models.BigIntegerField()
    updated_at = models.DateTimeField(auto_now=True)
    count = models.PositiveIntegerField(default=0)
