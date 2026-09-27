import base64
import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from django.conf import settings

from reaper.services.security import ServiceError

MANIFESTS = {
    "cargo.toml",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "pubspec.yaml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gemfile",
    "composer.json",
    "dockerfile",
    "compose.yaml",
    "docker-compose.yml",
    "vercel.json",
    "netlify.toml",
    "fly.toml",
    "render.yaml",
    "pytest.ini",
    "tox.ini",
    "jest.config.js",
    "vitest.config.ts",
}
LOCKFILES = {
    "cargo.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "pubspec.lock",
}
SOURCE_EXT = {".py", ".rs", ".ts", ".tsx", ".go", ".java", ".dart"}
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", ".git", ".venv", "coverage"}


def redact(text):
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
    )
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b",
        "[REDACTED TOKEN]",
        text,
    )
    return re.sub(
        r"(?im)([\w-]*(?:token|secret|password|api[_-]?key)[\w-]*\s*[:=]\s*)[^\n,}]+",
        r"\1[REDACTED]",
        text,
    )


def collect(github, metadata, sha, heartbeat=lambda: None):
    prefix = f"/repositories/{int(metadata['id'])}"
    warnings = []
    evidence = {
        "metadata": {
            k: metadata.get(k)
            for k in [
                "description",
                "language",
                "topics",
                "archived",
                "fork",
                "pushed_at",
                "created_at",
                "default_branch",
            ]
        },
        "commit_sha": sha,
        "files": {},
        "evidence_index": ["metadata"],
        "limitations": warnings,
    }

    def optional(path, label):
        heartbeat()
        try:
            data = github.get(prefix + path)
            evidence["evidence_index"].append(label)
            return data
        except ServiceError as exc:
            if exc.retry_after or "authorization" in str(exc):
                raise
            warnings.append(f"{label} unavailable; not evidence of absence.")
            return None

    evidence["languages"] = optional("/languages", "languages") or {}
    if not sha:
        warnings.append("Empty repository: no commit or files to inspect.")
        return finalize(evidence)
    tree = optional(f"/git/trees/{sha}?recursive=1", "tree") or {}
    entries = tree.get("tree", [])
    if tree.get("truncated"):
        warnings.append(
            "GitHub truncated the file tree; absent files may exist outside this sample."
        )
    paths = [
        entry["path"]
        for entry in entries
        if not SKIP_DIRS.intersection(PurePosixPath(entry["path"]).parts)
    ]
    evidence["tree"] = paths[:500]
    evidence["lockfiles"] = [p for p in paths if PurePosixPath(p).name.lower() in LOCKFILES][:30]
    evidence["major_directories"] = sorted({p.split("/")[0] for p in paths if "/" in p})[:60]
    if len(paths) > 500:
        warnings.append(
            "Only the first 500 paths are included; representative file selection uses the available tree."
        )

    def rank(entry):
        path = entry["path"].lower()
        name = PurePosixPath(path).name
        if name.startswith("readme"):
            return 0
        if name in MANIFESTS:
            return 1
        if path.startswith(".github/workflows/") or "test" in name or name.startswith("dockerfile"):
            return 2
        if path.startswith("docs/") and path.endswith(".md"):
            return 3
        if PurePosixPath(path).suffix in SOURCE_EXT:
            return 4
        return 9

    candidates = [
        e
        for e in entries
        if e.get("type") == "blob"
        and e.get("mode") == "100644"
        and e.get("size", 0) <= 100000
        and not SKIP_DIRS.intersection(PurePosixPath(e["path"]).parts)
        and rank(e) < 9
        and not re.search(r"(?i)(\.env|secret|credential|\.pem$|\.key$)", e["path"])
    ]
    for entry in sorted(candidates, key=lambda e: (rank(e), e["path"].count("/"), e["path"]))[:12]:
        heartbeat()
        blob = optional(f"/git/blobs/{quote(entry['sha'], safe='')}", entry["path"])
        if blob and blob.get("encoding") == "base64":
            try:
                content = base64.b64decode(blob["content"], validate=False).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                warnings.append(f"Skipped non-text file: {entry['path']}")
                continue
            evidence["files"][entry["path"]] = redact(content)[:4000]
            if len(content) > 4000:
                warnings.append(f"Excerpt truncated: {entry['path']}")
    commits = optional(f"/commits?sha={sha}&per_page=15", "commits") or []
    evidence["commits"] = [
        {
            "sha": c["sha"],
            "date": c["commit"]["committer"]["date"],
            "message": redact(c["commit"]["message"])[:200],
        }
        for c in commits
    ]
    meaningful = [
        c
        for c in evidence["commits"]
        if not re.match(r"(?i)^(merge|bump |chore\(deps\)|dependabot)", c["message"])
    ]
    evidence["latest_meaningful_commit_candidate"] = meaningful[0]["date"] if meaningful else None
    warnings.append(
        "Meaningful commit date is a message-based heuristic over the last 15 commits, not a full history audit."
    )
    releases = optional("/releases?per_page=3", "releases") or []
    evidence["releases"] = [
        {"tag": r.get("tag_name"), "published_at": r.get("published_at")} for r in releases
    ]
    issues = optional("/issues?state=open&per_page=5", "issues") or []
    evidence["issues"] = [
        {"number": i["number"], "title": redact(i["title"])[:200]}
        for i in issues
        if "pull_request" not in i
    ]
    warnings.append(
        "Files were sampled, not executed. Lockfile presence is recorded; dependency vulnerabilities and live deployments are not verified."
    )

    return finalize(evidence)


def finalize(evidence):
    # Redact metadata, filenames and nested values even for an empty repository.
    def clean(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {redact(k): clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    evidence = clean(evidence)
    limited = False
    while len(json.dumps(evidence, ensure_ascii=False)) > settings.MAX_EVIDENCE_CHARS - 200:
        if evidence["files"]:
            evidence["files"].pop(next(reversed(evidence["files"])))
        else:
            removable = next(
                (
                    key
                    for key in (
                        "tree",
                        "major_directories",
                        "lockfiles",
                        "commits",
                        "issues",
                        "releases",
                    )
                    if evidence.get(key)
                ),
                None,
            )
            if not removable:
                raise ServiceError("Repository metadata exceeds the safe evidence budget.")
            evidence[removable].pop()
        limited = True
        evidence["evidence_index"] = [
            key for key in evidence["evidence_index"] if key in evidence or key in evidence["files"]
        ]
    if limited:
        evidence["limitations"].append(
            "Evidence was trimmed to the input budget; omitted files and paths are not proof of absence."
        )
    evidence["evidence_index"] = [
        key for key in evidence["evidence_index"] if key in evidence or key in evidence["files"]
    ]
    return evidence


def fingerprint(evidence):
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
