import uuid
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from reaper.models import (
    ACTIVE_STATES,
    AIAccount,
    GitHubAccount,
    Repository,
    RepositoryAnalysis,
    RepositoryScan,
    User,
)
from reaper.services.analysis import AnalysisResult, OpenAIProvider
from reaper.services.evidence import collect, fingerprint
from reaper.services.github import GitHub, repository_fields
from reaper.services.security import ServiceError, decrypt


class LostLease(Exception):
    pass


def enqueue(user, repository):
    with transaction.atomic():
        owner = User.objects.select_for_update().get(pk=user.pk)
        repo = Repository.objects.get(pk=repository.pk, user=owner)
        if not owner.ai_consent:
            raise ServiceError("Enable AI analysis consent in Settings before scanning.")
        if not AIAccount.objects.filter(user=owner).exists():
            raise ServiceError("Add your OpenAI API key in Settings before scanning.")
        if not GitHubAccount.objects.filter(user=owner).exists():
            raise ServiceError("Connect GitHub before scanning.")
        if repo.private and not owner.allow_private:
            raise ServiceError("Enable private repository access in Settings before scanning.")
        active = RepositoryScan.objects.filter(
            user=owner, repository=repo, status__in=ACTIVE_STATES
        ).first()
        if active:
            return active
        now = timezone.now()
        if not owner.scan_budget_started_at or owner.scan_budget_started_at <= now - timedelta(
            days=1
        ):
            owner.scan_budget_started_at = now
            owner.scan_budget_used = 0
        if owner.scan_budget_used >= settings.DAILY_SCAN_LIMIT:
            raise ServiceError("Daily scan limit reached. Try again tomorrow.")
        owner.scan_budget_used += 1
        owner.save(update_fields=["scan_budget_started_at", "scan_budget_used"])
        return RepositoryScan.objects.create(
            user=owner,
            repository=repo,
            available_at=timezone.now(),
            analysis_version=settings.ANALYSIS_VERSION,
            model=owner.ai_model or settings.AI_MODELS[0],
        )


def claim_job():
    now = timezone.now()
    with transaction.atomic():
        expired = RepositoryScan.objects.filter(status__in=ACTIVE_STATES[1:], lease_until__lt=now)
        expired.filter(status="analyzing").update(
            status="failed",
            completed_at=now,
            error="Worker stopped during AI analysis. Retry manually to control duplicate charges.",
            lease_token=None,
        )
        expired.filter(attempts__gte=3).update(
            status="failed",
            completed_at=now,
            error="Worker recovery limit reached. Retry manually.",
            lease_token=None,
        )
        expired.exclude(status="analyzing").filter(attempts__lt=3).update(
            status="queued", available_at=now, lease_token=None
        )
        query = RepositoryScan.objects.filter(status="queued", available_at__lte=now).order_by(
            "created_at"
        )
        query = (
            query.select_for_update(skip_locked=True)
            if connection.vendor == "postgresql"
            else query.select_for_update()
        )
        scan = query.first()
        if not scan:
            return None
        scan.status = "fetching"
        scan.started_at = now
        scan.lease_until = now + timedelta(minutes=5)
        scan.lease_token = uuid.uuid4()
        scan.attempts += 1
        scan.save()
        return scan


def touch(scan, status=None):
    updates = {"lease_until": timezone.now() + timedelta(minutes=5)}
    if status:
        updates["status"] = status
    if not RepositoryScan.objects.filter(
        pk=scan.pk,
        user_id=scan.user_id,
        lease_token=scan.lease_token,
        status__in=ACTIVE_STATES,
        lease_until__gt=timezone.now(),
    ).update(**updates):
        raise LostLease()


def changes_between(previous, current):
    if not previous:
        return ["First analysis establishes the baseline."]
    labels = {
        "project_status": "Status",
        "resurrection_effort": "Effort",
        "portfolio_value": "Portfolio value",
        "suggested_next_step": "Next step",
        "detected_languages": "Languages",
        "detected_frameworks": "Frameworks",
        "technical_debt": "Technical debt",
        "security_concerns": "Security concerns",
        "resurrection_plan": "Resurrection plan",
    }
    return [
        f"{label}: {previous.get(key)} → {current.get(key)}"
        for key, label in labels.items()
        if previous.get(key) != current.get(key)
    ] or ["No material change in the audit conclusions."]


def run_job(scan, provider=None):
    try:
        user = User.objects.get(pk=scan.user_id)
        privacy_epoch = user.privacy_epoch
        repo = Repository.objects.get(pk=scan.repository_id, user=user)
        if not user.ai_consent:
            raise ServiceError("AI consent was withdrawn. Scan cancelled.")
        gh = GitHub(user.pk)
        metadata = gh.repository(repo.github_id)
        if metadata["id"] != repo.github_id:
            raise ServiceError("Repository identity verification failed.")
        if metadata["private"] and not user.allow_private:
            raise ServiceError("Private repository access is disabled. Enable it in Settings.")
        commit = gh.commit(repo.github_id, metadata.get("default_branch") or "main")
        sha = commit["sha"] if commit else ""
        touch(scan, "inspecting")
        evidence = collect(gh, metadata, sha, heartbeat=lambda: touch(scan))
        digest = fingerprint(evidence)
        previous = (
            RepositoryAnalysis.objects.filter(
                user=user, scan__repository=repo, scan__status="complete"
            )
            .select_related("scan")
            .order_by("-scan__completed_at")
            .first()
        )
        reusable = RepositoryAnalysis.objects.filter(
            user=user,
            scan__repository=repo,
            scan__status="complete",
            scan__content_hash=digest,
            scan__model=scan.model,
            scan__analysis_version=scan.analysis_version,
        ).first()
        # Recheck consent and cancellation immediately before crossing the AI boundary.
        user.refresh_from_db()
        if (
            not user.ai_consent
            or user.privacy_epoch != privacy_epoch
            or (metadata["private"] and not user.allow_private)
            or not GitHubAccount.objects.filter(user=user).exists()
            or not AIAccount.objects.filter(user=user).exists()
        ):
            raise ServiceError("Scan cancelled because privacy settings changed.")
        touch(scan, "analyzing")
        if reusable:
            result, inputs, outputs = AnalysisResult.model_validate(reusable.result), 0, 0
        else:
            if provider is None:
                account = AIAccount.objects.get(user=user)
                provider = OpenAIProvider(decrypt(account.api_key))
            result, inputs, outputs = provider.analyze(evidence, scan.model)
        with transaction.atomic():
            # Serialize against deletion and privacy actions; never recreate deleted data.
            owner = User.objects.select_for_update().get(pk=scan.user_id)
            current = (
                RepositoryScan.objects.select_for_update()
                .filter(
                    pk=scan.pk,
                    user=owner,
                    lease_token=scan.lease_token,
                    status="analyzing",
                    lease_until__gt=timezone.now(),
                )
                .first()
            )
            if (
                not current
                or not owner.ai_consent
                or owner.privacy_epoch != privacy_epoch
                or (metadata["private"] and not owner.allow_private)
                or not GitHubAccount.objects.filter(user=owner).exists()
                or not AIAccount.objects.filter(user=owner).exists()
            ):
                raise LostLease()
            Repository.objects.filter(pk=repo.pk, user=owner).update(
                **repository_fields(metadata), latest_commit_sha=sha
            )
            data = result.model_dump()
            RepositoryAnalysis.objects.create(
                scan=current,
                user=owner,
                project_status=result.project_status,
                resurrection_effort=result.resurrection_effort,
                portfolio_value=result.portfolio_value,
                result=data,
            )
            current.status = "complete"
            current.completed_at = timezone.now()
            current.commit_sha = sha
            current.content_hash = digest
            current.cached = bool(reusable)
            current.input_tokens = inputs
            current.output_tokens = outputs
            # Store evidence inventory and facts, not source excerpts.
            current.evidence = {k: v for k, v in evidence.items() if k != "files"}
            current.changes = changes_between(previous.result if previous else None, data)
            current.lease_token = None
            current.error = ""
            current.save()
    except (LostLease, User.DoesNotExist, Repository.DoesNotExist):
        return
    except Exception as exc:
        safe = (
            str(exc)
            if isinstance(exc, ServiceError)
            else "Scan failed unexpectedly. Please retry; contact the operator if it persists."
        )
        retry = exc.retry_after if isinstance(exc, ServiceError) else None
        updates = {
            "status": "failed",
            "error": safe[:300],
            "completed_at": timezone.now(),
            "lease_token": None,
        }
        if retry and scan.attempts < 4:
            updates.update(
                status="queued",
                available_at=timezone.now() + timedelta(seconds=retry),
                completed_at=None,
            )
        RepositoryScan.objects.filter(
            pk=scan.pk, user_id=scan.user_id, lease_token=scan.lease_token
        ).update(**updates)
