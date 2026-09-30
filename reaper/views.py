import base64
import hashlib
import json
import logging
import secrets
import time
from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from reaper.models import (
    ACTIVE_STATES,
    AIAccount,
    GitHubAccount,
    GitHubCache,
    Repository,
    RepositoryScan,
    ResurrectionQueueItem,
    User,
)
from reaper.services.analysis import EFFORTS, STATUSES, VALUES
from reaper.services.github import GitHub, repository_fields, revoke, save_tokens, token_exchange
from reaper.services.jobs import enqueue
from reaper.services.security import ServiceError, decrypt, encrypt, rate_limit

oauth_logger = logging.getLogger("reaper.oauth")


def oauth_callback_uri():
    # One canonical public origin for authorization and token exchange, independent of proxy hosts.
    return settings.APP_URL + reverse("oauth_callback")


def landing(request):
    return render(request, "reaper/landing.html")


def login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "reaper/login.html")


@require_http_methods(["GET", "POST"])
@rate_limit("oauth", 10, 300)
def oauth_start(request):
    if not settings.GITHUB_CLIENT_ID or not settings.GITHUB_CLIENT_SECRET:
        messages.error(
            request, "GitHub sign-in needs configuration. See the setup instructions in the README."
        )
        return redirect("login")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    request.session["oauth"] = {"state": state, "verifier": verifier, "issued": time.time()}
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": oauth_callback_uri(),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return redirect("https://github.com/login/oauth/authorize?" + urlencode(params))


@require_GET
@rate_limit("callback", 20, 300)
def oauth_callback(request):
    pending = request.session.pop("oauth", None)
    if (
        not pending
        or time.time() - pending["issued"] > 600
        or not secrets.compare_digest(pending["state"], request.GET.get("state", ""))
        or not request.GET.get("code")
        or request.GET.get("error")
    ):
        oauth_logger.info("oauth_callback rejected_state_or_provider_denial")
        messages.error(request, "GitHub sign-in expired or was declined. Please try again.")
        return redirect("login")
    try:
        data = token_exchange(
            {
                "code": request.GET["code"],
                "code_verifier": pending["verifier"],
                "redirect_uri": oauth_callback_uri(),
            }
        )
        profile = GitHub(token=data["access_token"]).get("/user")
        with transaction.atomic():
            user, created = User.objects.get_or_create(
                github_user_id=profile["id"], defaults={"username": f"github_{profile['id']}"}
            )
            user = User.objects.select_for_update().get(pk=user.pk)
            if request.user.is_authenticated and request.user.pk != user.pk:
                raise ServiceError(
                    "Reconnect with the GitHub account already linked to this session, or sign out first."
                )
            if created:
                user.set_unusable_password()
            user.github_username = profile["login"]
            user.display_name = profile.get("name") or ""
            avatar = profile.get("avatar_url", "")
            user.avatar_url = (
                avatar if avatar.startswith("https://avatars.githubusercontent.com/") else ""
            )
            user.save()
            account, _ = GitHubAccount.objects.get_or_create(user=user)
            save_tokens(account, data)
            GitHubCache.objects.filter(user=user).delete()
        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        oauth_logger.info("oauth_callback authenticated")
        return redirect("dashboard")
    except (ServiceError, KeyError, ValueError):
        oauth_logger.warning("oauth_callback exchange_or_identity_failed")
        messages.error(
            request,
            "GitHub sign-in could not be completed. Check the app configuration and try again.",
        )
        return redirect("login")


@require_POST
def logout(request):
    auth_logout(request)
    return redirect("landing")


def rows_for(user):
    scans = (
        RepositoryScan.objects.filter(user=user).select_related("analysis").order_by("-created_at")
    )
    repos = Repository.objects.filter(user=user).prefetch_related(
        Prefetch("scans", queryset=scans, to_attr="history"),
        Prefetch(
            "queue_items",
            queryset=ResurrectionQueueItem.objects.filter(user=user),
            to_attr="personal_queue",
        ),
    )
    rows = []
    for repo in repos:
        completed = next(
            (s for s in repo.history if s.status == "complete" and hasattr(s, "analysis")), None
        )
        rows.append(
            {
                "repo": repo,
                "scan": completed,
                "latest": repo.history[0] if repo.history else None,
                "a": completed.analysis.result if completed else None,
                "queue": repo.personal_queue[0] if repo.personal_queue else None,
            }
        )
    return rows


def filter_rows(rows, query):
    filtered = []
    for row in rows:
        repo, a = row["repo"], row["a"] or {}
        text = " ".join(
            [repo.full_name, repo.description, a.get("summary", ""), a.get("original_purpose", "")]
        ).lower()
        if query.get("q", "").lower() not in text:
            continue
        checks = {
            "status": [a.get("project_status")],
            "language": a.get("detected_languages", [repo.language]),
            "framework": a.get("detected_frameworks", []),
            "value": [a.get("portfolio_value")],
            "effort": [a.get("resurrection_effort")],
            "visibility": ["private" if repo.private else "public"],
            "archived": ["yes" if repo.archived else "no"],
        }
        if any(query.get(key) and query[key] not in values for key, values in checks.items()):
            continue
        filtered.append(row)
    sort = query.get("sort", "updated")
    keys = {
        "name": lambda r: r["repo"].name.lower(),
        "updated": lambda r: r["repo"].pushed_at.timestamp() if r["repo"].pushed_at else 0,
        "scanned": lambda r: r["scan"].completed_at.timestamp() if r["scan"] else 0,
        "value": lambda r: VALUES.index((r["a"] or {}).get("portfolio_value", "Low")),
        "effort": lambda r: EFFORTS.index(
            (r["a"] or {}).get("resurrection_effort", "Rewrite Recommended")
        ),
        "status": lambda r: (r["a"] or {}).get("project_status", "Unknown"),
        "priority": lambda r: (
            ["Next", "Soon", "Someday"].index(r["queue"].priority) if r["queue"] else 3
        ),
    }
    return sorted(
        filtered,
        key=keys.get(sort, keys["name"]),
        reverse=query.get("direction") == "desc"
        or (sort in ("updated", "scanned") and query.get("direction") != "asc"),
    )


def dashboard_context(user, query):
    rows = rows_for(user)
    analyzed = [r for r in rows if r["a"]]
    languages = Counter(lang for r in analyzed for lang in r["a"]["detected_languages"])
    frameworks = Counter(fw for r in analyzed for fw in r["a"]["detected_frameworks"])
    return {
        "rows": filter_rows(rows, query),
        "total": len(analyzed),
        "shipped": sum(r["a"]["project_status"] == "Production / Shipped" for r in analyzed),
        "nearly": sum(r["a"]["project_status"] == "Nearly Complete" for r in analyzed),
        "abandoned": sum(r["a"]["project_status"] == "Abandoned" for r in analyzed),
        "high": sum(r["a"]["portfolio_value"] == "High" for r in analyzed),
        "quick": sum(r["a"]["resurrection_effort"] in ("Tiny", "Small") for r in analyzed),
        "top_languages": languages.most_common(3),
        "top_frameworks": frameworks.most_common(3),
        "languages": sorted(languages),
        "frameworks": sorted(frameworks),
        "statuses": STATUSES,
        "efforts": EFFORTS,
        "values": VALUES,
        "active": ACTIVE_STATES,
        "filters": query,
    }


@login_required
@require_GET
def dashboard(request):
    context = dashboard_context(request.user, request.GET)
    context["page"] = Paginator(context["rows"], 30).get_page(request.GET.get("page"))
    return render(request, "reaper/dashboard.html", context)


@login_required
@require_GET
@rate_limit("discovery", 30)
def repositories(request):
    ai_key_configured = AIAccount.objects.filter(user=request.user).exists()
    try:
        page = max(1, min(1000, int(request.GET.get("page", 1))))
    except ValueError:
        page = 1
    try:
        github = GitHub(request.user.pk)
        data = github.discover(page)
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            if (
                user.privacy_epoch != github.privacy_epoch
                or not GitHubAccount.objects.filter(user=user).exists()
            ):
                raise ServiceError("GitHub is disconnected. Reconnect in Settings.")
            # The snapshot is the server-side allowlist for the next selection POST.
            GitHubCache.objects.update_or_create(
                user=user,
                key="discovery",
                defaults={
                    "body": encrypt(json.dumps(data)),
                    "expires_at": timezone.now() + timedelta(minutes=15),
                },
            )
        visible = [r for r in data if not r["private"] or request.user.allow_private]
        languages = sorted({r["language"] for r in visible if r.get("language")})
        rows = [
            r
            for r in visible
            if request.GET.get("q", "").lower()
            in (r["full_name"] + " " + (r.get("description") or "")).lower()
            and (not request.GET.get("language") or r.get("language") == request.GET["language"])
            and (
                not request.GET.get("visibility")
                or r["private"] == (request.GET["visibility"] == "private")
            )
            and (
                not request.GET.get("archived")
                or r.get("archived", False) == (request.GET["archived"] == "yes")
            )
        ]
        return render(
            request,
            "reaper/repositories.html",
            {
                "repos": rows,
                "languages": languages,
                "page_number": page,
                "next_page": page + 1 if len(data) == 100 else None,
                "previous_page": page - 1 if page > 1 else None,
                "filters": request.GET,
                "ai_key_configured": ai_key_configured,
            },
        )
    except ServiceError as exc:
        return render(
            request,
            "reaper/repositories.html",
            {
                "error": str(exc),
                "filters": request.GET,
                "ai_key_configured": ai_key_configured,
            },
        )


@login_required
@require_POST
@rate_limit("import", 10)
def import_repositories(request):
    ids = request.POST.getlist("repositories")
    if not ids or len(ids) > settings.MAX_BATCH_SIZE or any(not i.isdecimal() for i in ids):
        messages.error(request, "Select between 1 and 20 repositories.")
        return redirect("repositories")
    snapshot = GitHubCache.objects.filter(
        user=request.user, key="discovery", expires_at__gt=timezone.now()
    ).first()
    allowed = {str(r["id"]): r for r in json.loads(decrypt(snapshot.body))} if snapshot else {}
    if not set(ids).issubset(allowed):
        return HttpResponse(
            "Repository selection expired or is not authorized. Refresh the selection page.",
            status=403,
        )
    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            if not GitHubCache.objects.filter(
                pk=snapshot.pk, user=user, expires_at__gt=timezone.now()
            ).exists():
                raise ServiceError(
                    "Selection was cleared by a privacy change. Refresh repositories before scanning."
                )
            for github_id in set(ids):
                metadata = allowed[github_id]
                if metadata["private"] and not user.allow_private:
                    raise ServiceError("Enable private access in Settings first.")
                repo, _ = Repository.objects.update_or_create(
                    user=user, github_id=int(github_id), defaults=repository_fields(metadata)
                )
                enqueue(user, repo)
        messages.success(request, f"{len(set(ids))} repositories queued. Let the excavation begin.")
        return redirect("dashboard")
    except ServiceError as exc:
        messages.error(request, str(exc))
        return redirect("repositories")


@login_required
@require_GET
def repo_detail(request, repo_id):
    repo = get_object_or_404(Repository, pk=repo_id, user=request.user)
    scans = list(
        RepositoryScan.objects.filter(repository=repo, user=request.user).select_related("analysis")
    )
    selected = next(
        (s for s in scans if str(s.pk) == request.GET.get("scan") and s.status == "complete"), None
    )
    if request.GET.get("scan") and not selected:
        return HttpResponse("Scan not found.", status=404)
    selected = selected or next((s for s in scans if s.status == "complete"), None)
    return render(
        request,
        "reaper/detail.html",
        {
            "repo": repo,
            "scans": scans,
            "scan": selected,
            "a": selected.analysis.result if selected else None,
            "latest": scans[0] if scans else None,
            "active": ACTIVE_STATES,
            "queue_item": ResurrectionQueueItem.objects.filter(
                user=request.user, repository=repo
            ).first(),
        },
    )


@login_required
@require_GET
def scan_status(request, scan_id):
    scan = get_object_or_404(RepositoryScan, pk=scan_id, user=request.user)
    return JsonResponse(
        {
            "id": str(scan.pk),
            "status": scan.status,
            "label": scan.get_status_display(),
            "error": scan.error,
            "cached": scan.cached,
        }
    )


@login_required
@require_POST
@rate_limit("rescan", 10)
def rescan(request, repo_id):
    repo = get_object_or_404(Repository, pk=repo_id, user=request.user)
    try:
        enqueue(request.user, repo)
        messages.success(
            request, "Rescan queued. Unchanged evidence reuses your existing AI analysis."
        )
    except ServiceError as exc:
        messages.error(request, str(exc))
    return redirect("repo_detail", repo_id=repo.pk)


@login_required
@require_GET
def graveyard(request):
    context = dashboard_context(request.user, request.GET)
    context["rows"] = [
        r
        for r in context["rows"]
        if r["a"]
        and r["a"]["project_status"]
        in ("Abandoned", "Early Prototype", "Functional Prototype", "Nearly Complete")
        and r["a"]["portfolio_value"] in ("High", "Medium")
    ]
    return render(request, "reaper/graveyard.html", context)


@login_required
@require_GET
def queue(request):
    query = request.GET.copy()
    if not query.get("sort"):
        query["sort"] = "priority"
    context = dashboard_context(request.user, query)
    context["rows"] = [r for r in context["rows"] if r["queue"]]
    return render(request, "reaper/queue.html", context)


@login_required
@require_POST
def queue_update(request, repo_id):
    repo = get_object_or_404(Repository, pk=repo_id, user=request.user)
    priority = request.POST.get("priority", "Soon")
    if priority not in ("Next", "Soon", "Someday") or len(request.POST.get("notes", "")) > 2000:
        return HttpResponse("Invalid queue settings.", status=400)
    with transaction.atomic():
        User.objects.select_for_update().get(pk=request.user.pk)
        repo = get_object_or_404(Repository, pk=repo_id, user=request.user)
        if request.POST.get("action") == "remove":
            ResurrectionQueueItem.objects.filter(user=request.user, repository=repo).delete()
        else:
            item, _ = ResurrectionQueueItem.objects.get_or_create(
                user=request.user, repository=repo
            )
            if "priority" in request.POST:
                item.priority = priority
            if "notes" in request.POST:
                item.notes = request.POST["notes"]
            item.save()
    return redirect("queue")


@login_required
@require_POST
def delete_repository(request, repo_id):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        user.privacy_epoch += 1
        user.save(update_fields=["privacy_epoch"])
        repo = get_object_or_404(Repository, pk=repo_id, user=user)
        repo.delete()
        GitHubCache.objects.filter(user=request.user).delete()
    messages.success(
        request,
        "Repository analysis and scan history deleted. Your GitHub repository is untouched.",
    )
    return redirect("dashboard")


@login_required
@require_GET
def account_settings(request):
    ai_account = AIAccount.objects.filter(user=request.user).first()
    return render(
        request,
        "reaper/settings.html",
        {
            "connected": GitHubAccount.objects.filter(user=request.user).exists(),
            "daily_limit": settings.DAILY_SCAN_LIMIT,
            "ai_key_configured": bool(ai_account),
            "ai_key_hint": ai_account.key_hint if ai_account else "",
        },
    )


@login_required
@require_POST
def save_settings(request):
    model = request.POST.get("model", "")
    if model not in settings.AI_MODELS:
        return HttpResponse("Invalid model.", status=400)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        user.privacy_epoch += 1
        user.ai_model = model
        user.ai_consent = request.POST.get("ai_consent") == "on"
        user.allow_private = request.POST.get("allow_private") == "on"
        user.save()
        jobs = RepositoryScan.objects.filter(user=user, status__in=ACTIVE_STATES)
        if not user.ai_consent:
            jobs.update(
                status="failed",
                completed_at=timezone.now(),
                lease_token=None,
                error="Cancelled: AI consent withdrawn.",
            )
        elif not user.allow_private:
            jobs.filter(repository__private=True).update(
                status="failed",
                completed_at=timezone.now(),
                lease_token=None,
                error="Cancelled: private repository access disabled.",
            )
        GitHubCache.objects.filter(user=user).delete()
    messages.success(request, "Preferences saved.")
    return redirect("settings")


@login_required
@require_POST
@rate_limit("ai-key", 10, 300)
def save_ai_key(request):
    api_key = request.POST.get("api_key", "").strip()
    if len(api_key) < 20 or len(api_key) > 512 or any(char.isspace() for char in api_key):
        return HttpResponse("Enter a valid OpenAI API key.", status=400)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        user.privacy_epoch += 1
        user.save(update_fields=["privacy_epoch"])
        AIAccount.objects.update_or_create(
            user=user,
            defaults={"api_key": encrypt(api_key), "key_hint": f"••••{api_key[-4:]}"},
        )
        RepositoryScan.objects.filter(user=user, status__in=ACTIVE_STATES).update(
            status="failed",
            completed_at=timezone.now(),
            lease_token=None,
            error="Cancelled: AI credential changed. Retry the scan.",
        )
    messages.success(
        request,
        "OpenAI API key saved securely. It will be checked by OpenAI during your next uncached analysis.",
    )
    return redirect("settings")


@login_required
@require_POST
@rate_limit("ai-key", 10, 300)
def delete_ai_key(request):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        user.privacy_epoch += 1
        user.save(update_fields=["privacy_epoch"])
        AIAccount.objects.filter(user=user).delete()
        RepositoryScan.objects.filter(user=user, status__in=ACTIVE_STATES).update(
            status="failed",
            completed_at=timezone.now(),
            lease_token=None,
            error="Cancelled: OpenAI API key removed.",
        )
    messages.success(request, "OpenAI API key removed.")
    return redirect("settings")


@login_required
@require_POST
@rate_limit("privacy", 10)
def privacy_action(request):
    action = request.POST.get("action")
    expected = {
        "delete_all": "DELETE ANALYSES",
        "disconnect": "DISCONNECT",
        "delete_account": "DELETE ACCOUNT",
    }
    if action not in expected or request.POST.get("confirmation") != expected[action]:
        messages.error(request, "Type the exact confirmation phrase to continue.")
        return redirect("settings")
    revoked = True
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        user.privacy_epoch += 1
        user.save(update_fields=["privacy_epoch"])
        if action in ("disconnect", "delete_account"):
            revoked = revoke(user.pk)
            GitHubAccount.objects.filter(user=user).delete()
            RepositoryScan.objects.filter(user=user, status__in=ACTIVE_STATES).update(
                status="failed",
                lease_token=None,
                completed_at=timezone.now(),
                error="Cancelled: GitHub disconnected.",
            )
        GitHubCache.objects.filter(user=user).delete()
        if action in ("delete_all", "delete_account"):
            Repository.objects.filter(user=user).delete()
        if action == "delete_account":
            user.delete()
    if action == "delete_account":
        auth_logout(request)
    if not revoked:
        messages.warning(
            request,
            "Local credentials removed. GitHub revocation could not be confirmed; revoke the app in GitHub Settings → Applications.",
        )
    else:
        messages.success(request, "Privacy action completed. No GitHub repositories were modified.")
    return redirect("landing" if action == "delete_account" else "settings")


def health(request):
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
