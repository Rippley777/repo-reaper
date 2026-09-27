import hashlib
import json
import time
from datetime import timedelta
from urllib.parse import quote

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from reaper.models import GitHubAccount, GitHubCache, User
from reaper.services.security import ServiceError, decrypt, encrypt

API = "https://api.github.com"
MAX_RESPONSE = 6 * 1024 * 1024


def token_exchange(payload):
    try:
        response = httpx.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                **payload,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("access_token", "").startswith("ghu_") or data.get("scope"):
            raise ServiceError(
                "GitHub App authorization failed. Reconnect with the configured read-only GitHub App."
            )
        return data
    except (httpx.HTTPError, ValueError) as exc:
        raise ServiceError("GitHub authorization is unavailable. Please sign in again.") from exc


def save_tokens(account, data):
    account.access_token = encrypt(data["access_token"])
    account.refresh_token = encrypt(data.get("refresh_token", ""))
    account.expires_at = (
        timezone.now() + timedelta(seconds=int(data["expires_in"]))
        if data.get("expires_in")
        else None
    )
    account.refresh_expires_at = (
        timezone.now() + timedelta(seconds=int(data["refresh_token_expires_in"]))
        if data.get("refresh_token_expires_in")
        else None
    )
    account.scopes = data.get("scope", "")
    account.save()


def user_token(user_id):
    with transaction.atomic():
        account = GitHubAccount.objects.select_for_update().filter(user_id=user_id).first()
        if not account:
            raise ServiceError("GitHub is disconnected. Reconnect in Settings.")
        if account.expires_at and account.expires_at <= timezone.now() + timedelta(minutes=2):
            if not account.refresh_token or (
                account.refresh_expires_at and account.refresh_expires_at <= timezone.now()
            ):
                raise ServiceError("GitHub authorization expired. Please reconnect in Settings.")
            save_tokens(
                account,
                token_exchange(
                    {"grant_type": "refresh_token", "refresh_token": decrypt(account.refresh_token)}
                ),
            )
        return decrypt(account.access_token)


class GitHub:
    def __init__(self, user_id=None, token=None):
        self.user_id = user_id
        self.token = token if token else user_token(user_id)
        self.memo = {}
        self.privacy_epoch = User.objects.get(pk=user_id).privacy_epoch if user_id else None

    def get(self, path, ttl=300, fresh=False):
        if not path.startswith("/") or path.startswith("//") or ".." in path or "://" in path:
            raise ServiceError("Invalid GitHub resource.")
        if path in self.memo and not fresh:
            return self.memo[path]
        key = hashlib.sha256(path.encode()).hexdigest()
        cached = (
            GitHubCache.objects.filter(user_id=self.user_id, key=key).first()
            if self.user_id
            else None
        )
        if cached and cached.expires_at > timezone.now() and not fresh:
            data = json.loads(decrypt(cached.body))
            self.memo[path] = data
            return data
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        try:
            with httpx.stream(
                "GET", API + path, headers=headers, timeout=20, follow_redirects=False
            ) as response:
                # Numeric repository API routes avoid renamed-owner redirects.
                if response.status_code == 304 and cached:
                    data = json.loads(decrypt(cached.body))
                else:
                    if response.status_code == 401:
                        raise ServiceError(
                            "GitHub authorization was revoked. Reconnect in Settings."
                        )
                    if response.status_code in (403, 429):
                        if (
                            response.headers.get("x-ratelimit-remaining") == "0"
                            or "retry-after" in response.headers
                            or response.status_code == 429
                        ):
                            retry = max(
                                60,
                                int(response.headers.get("retry-after", "0")),
                                int(response.headers.get("x-ratelimit-reset", "0"))
                                - int(time.time()),
                            )
                            raise ServiceError(
                                "GitHub rate limit reached. The scan will retry automatically.",
                                min(retry, 86400),
                            )
                        raise ServiceError(
                            "GitHub denied access. Check your app installation and repository permissions."
                        )
                    if response.status_code in (404, 410):
                        raise ServiceError(
                            "Repository or evidence is unavailable. It may be deleted or access may have changed."
                        )
                    if response.status_code == 409:
                        return None  # Empty repository.
                    if response.status_code >= 500:
                        raise ServiceError(
                            "GitHub is temporarily unavailable. The scan will retry.", 60
                        )
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE:
                            raise ServiceError("GitHub response exceeded the safe size limit.")
                        chunks.append(chunk)
                    data = json.loads(b"".join(chunks))
                if self.user_id:
                    # Never recreate cache after disconnect/delete.
                    with transaction.atomic():
                        owner = User.objects.select_for_update().filter(pk=self.user_id).first()
                        if (
                            owner
                            and owner.privacy_epoch == self.privacy_epoch
                            and GitHubAccount.objects.filter(user_id=self.user_id).exists()
                        ):
                            GitHubCache.objects.update_or_create(
                                user_id=self.user_id,
                                key=key,
                                defaults={
                                    "body": encrypt(json.dumps(data)),
                                    "etag": response.headers.get(
                                        "etag", cached.etag if cached else ""
                                    ),
                                    "expires_at": timezone.now() + timedelta(seconds=ttl),
                                },
                            )
                self.memo[path] = data
                return data
        except (httpx.HTTPError, ValueError) as exc:
            raise ServiceError(
                "GitHub could not complete the request. Please retry shortly.", 60
            ) from exc

    def discover(self, page=1):
        return self.get(
            f"/user/repos?per_page=100&page={page}&sort=pushed&affiliation=owner,collaborator,organization_member",
            ttl=120,
        )

    def repository(self, github_id):
        return self.get(f"/repositories/{int(github_id)}", fresh=True)

    def commit(self, github_id, branch):
        return self.get(
            f"/repositories/{int(github_id)}/commits/{quote(branch, safe='')}", fresh=True
        )


def repository_fields(data):
    return {
        "owner": data["owner"]["login"],
        "name": data["name"],
        "description": (data.get("description") or "")[:4000],
        "private": bool(data["private"]),
        "archived": bool(data.get("archived")),
        "fork": bool(data.get("fork")),
        "language": data.get("language") or "",
        "default_branch": data.get("default_branch") or "main",
        "stars": max(0, data.get("stargazers_count", 0)),
        "pushed_at": parse_datetime(data["pushed_at"]) if data.get("pushed_at") else None,
        "metadata": {
            "topics": data.get("topics", []),
            "size": data.get("size", 0),
            "license": (data.get("license") or {}).get("spdx_id"),
            "open_issues": data.get("open_issues_count", 0),
        },
    }


def revoke(user_id):
    account = GitHubAccount.objects.filter(user_id=user_id).first()
    if not account:
        return True
    try:
        response = httpx.request(
            "DELETE",
            f"{API}/applications/{settings.GITHUB_CLIENT_ID}/grant",
            auth=(settings.GITHUB_CLIENT_ID, settings.GITHUB_CLIENT_SECRET),
            json={"access_token": decrypt(account.access_token)},
            timeout=15,
        )
        return response.status_code in (204, 404)
    except httpx.HTTPError:
        return False
