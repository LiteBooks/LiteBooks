"""Software update checking and request handling.

The web container cannot update itself: it runs unprivileged, carries no git
checkout, and has no access to the Docker socket. This module therefore does two
things only -- it works out whether a newer build exists upstream, and it drops a
request file into the shared state volume for the privileged updater sidecar to
act on. Everything that actually touches Docker lives in docker/updater/.
"""

import json
import os
import tempfile
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import SystemUpdateRun, SystemUpdateState
from .services import record_audit


GITHUB_API = "https://api.github.com"
GHCR_HOST = "https://ghcr.io"
SHORT_SHA_LENGTH = 7


def short_sha(sha):
    return (sha or "")[:SHORT_SHA_LENGTH]


def tag_for(sha):
    return f"sha-{short_sha(sha)}"


def state_dir():
    return settings.LITEBOOKS_UPDATE_STATE_DIR


def request_path():
    return os.path.join(state_dir(), "request.json")


def status_path():
    return os.path.join(state_dir(), "status.json")


def current_version():
    sha = settings.LITEBOOKS_GIT_SHA
    return {
        "version": settings.LITEBOOKS_VERSION,
        "sha": sha,
        "short_sha": short_sha(sha) if sha != "unknown" else "unknown",
        "built_at": settings.LITEBOOKS_BUILD_TIME,
        "image": settings.LITEBOOKS_UPDATE_IMAGE,
    }


def _get_json(url, headers=None):
    request_headers = {"Accept": "application/vnd.github+json", "User-Agent": "LiteBooks-Updater"}
    if settings.LITEBOOKS_GITHUB_TOKEN:
        request_headers["Authorization"] = f"Bearer {settings.LITEBOOKS_GITHUB_TOKEN}"
    request_headers.update(headers or {})
    with urlopen(Request(url, headers=request_headers), timeout=settings.LITEBOOKS_UPDATE_HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_github_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def image_published(sha):
    """Has CI finished pushing the image for this commit?

    Without this guard the update button is offered during the few minutes
    between a push landing on main and the workflow publishing its image, and
    the sidecar would fail on `docker pull`.
    """
    image = settings.LITEBOOKS_UPDATE_IMAGE
    if "/" not in image:
        return False
    repository = image.split("/", 1)[1]
    try:
        token = _get_json(
            f"{GHCR_HOST}/token?scope=repository:{repository}:pull&service=ghcr.io",
            headers={"Accept": "application/json"},
        ).get("token", "")
        manifest_request = Request(
            f"{GHCR_HOST}/v2/{repository}/manifests/{tag_for(sha)}",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
                "User-Agent": "LiteBooks-Updater",
            },
        )
        with urlopen(manifest_request, timeout=settings.LITEBOOKS_UPDATE_HTTP_TIMEOUT) as response:
            return response.status == 200
    except (HTTPError, URLError, OSError, ValueError, KeyError):
        return False


def _should_check(state, force):
    if force or state.last_checked_at is None:
        return True
    age = (timezone.now() - state.last_checked_at).total_seconds()
    return age >= settings.LITEBOOKS_UPDATE_CHECK_INTERVAL


def check_github(force=False):
    """Refresh the cached upstream state, swallowing every network failure.

    This runs inside an ordinary page-load request, so it must never raise and
    never hang: a box with no outbound internet should render the updates page
    with an explanatory error, not a 500.
    """
    state = SystemUpdateState.load()
    if not settings.LITEBOOKS_UPDATES_ENABLED or not _should_check(state, force):
        return state

    repo = settings.LITEBOOKS_UPDATE_REPO
    branch = settings.LITEBOOKS_UPDATE_BRANCH
    running_sha = settings.LITEBOOKS_GIT_SHA
    state.last_checked_at = timezone.now()

    try:
        head = _get_json(f"{GITHUB_API}/repos/{repo}/commits/{branch}")
        state.latest_sha = head.get("sha", "")
        commit = head.get("commit", {})
        state.latest_message = (commit.get("message", "").splitlines() or [""])[0][:300]
        state.latest_committed_at = _parse_github_time(commit.get("committer", {}).get("date"))
        state.check_error = ""
    except (HTTPError, URLError, OSError, ValueError, KeyError) as error:
        state.check_error = f"Could not reach GitHub: {error}"[:300]
        state.save()
        return state

    if running_sha in ("", "unknown") or running_sha == state.latest_sha:
        state.commits_behind = 0
        state.commit_log = []
    else:
        try:
            comparison = _get_json(f"{GITHUB_API}/repos/{repo}/compare/{running_sha}...{state.latest_sha}")
            state.commits_behind = comparison.get("ahead_by", 0)
            state.commit_log = [
                {
                    "sha": short_sha(item.get("sha", "")),
                    "message": (item.get("commit", {}).get("message", "").splitlines() or [""])[0][:200],
                    "date": item.get("commit", {}).get("committer", {}).get("date", ""),
                }
                for item in reversed(comparison.get("commits", []))
            ][:50]
        except (HTTPError, URLError, OSError, ValueError, KeyError):
            # The running sha may not exist upstream (a local build, or a force
            # push). A newer head is still worth offering, just without a diff.
            state.commits_behind = 1
            state.commit_log = []

    state.image_available = bool(state.latest_sha) and image_published(state.latest_sha)
    state.save()
    return state


def update_available(state=None):
    state = state or SystemUpdateState.load()
    if not settings.LITEBOOKS_UPDATES_ENABLED or not state.latest_sha:
        return False
    if state.latest_sha == settings.LITEBOOKS_GIT_SHA:
        return False
    if state.latest_sha == state.dismissed_sha:
        return False
    return state.commits_behind > 0 and state.image_available


def active_run():
    return SystemUpdateRun.objects.filter(status__in=SystemUpdateRun.ACTIVE_STATUSES).first()


def _write_atomic(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".request-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(payload, stream)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    except Exception:
        os.unlink(temp_path)
        raise


def request_update(user, target_sha):
    if not settings.LITEBOOKS_UPDATES_ENABLED:
        raise ValidationError("Software updates are disabled on this installation.")

    state = SystemUpdateState.load()
    if not target_sha or target_sha != state.latest_sha:
        raise ValidationError("That version is no longer the latest. Check for updates again.")
    if not update_available(state):
        raise ValidationError("There is no update to apply.")
    if active_run():
        raise ValidationError("An update is already in progress.")

    run = SystemUpdateRun.objects.create(
        requested_by=user,
        from_sha=settings.LITEBOOKS_GIT_SHA,
        to_sha=target_sha,
        to_tag=tag_for(target_sha),
        status=SystemUpdateRun.Status.PENDING,
        step="queued",
    )

    try:
        _write_atomic(request_path(), {
            "id": run.pk,
            "target_sha": target_sha,
            "target_tag": run.to_tag,
            "from_sha": run.from_sha,
            "requested_at": run.requested_at.isoformat(),
        })
    except OSError as error:
        run.status = SystemUpdateRun.Status.FAILED
        run.step = "queue"
        run.log = f"Could not reach the updater service: {error}\n\nIs the `updater` container running?"
        run.finished_at = timezone.now()
        run.save()
        raise ValidationError("Could not reach the updater service. Check that the updater container is running.") from error

    record_audit(user, "requested update", run, after={"to_sha": target_sha, "to_tag": run.to_tag})
    return run


def read_status():
    """Reconcile the sidecar's status file into the run row and return the run."""
    run = SystemUpdateRun.objects.first()
    try:
        with open(status_path()) as stream:
            status = json.load(stream)
    except (OSError, ValueError):
        return run

    if not run or status.get("id") != run.pk:
        return run

    reported = status.get("state", "")
    if reported in SystemUpdateRun.Status.values:
        run.status = reported
    run.step = (status.get("step") or "")[:40]
    log_lines = status.get("log") or []
    run.log = "\n".join(log_lines) if isinstance(log_lines, list) else str(log_lines)
    run.backup_path = (status.get("backup_path") or "")[:300]
    if not run.is_active and not run.finished_at:
        run.finished_at = timezone.now()
    run.save()
    return run


def run_json(run):
    if not run:
        return None
    return {
        "id": run.pk,
        "status": run.status,
        "status_label": run.get_status_display(),
        "step": run.step,
        "log": run.log,
        "backup_path": run.backup_path,
        "from_sha": short_sha(run.from_sha),
        "to_sha": short_sha(run.to_sha),
        "to_tag": run.to_tag,
        "requested_at": run.requested_at.isoformat(),
        "requested_by": run.requested_by.get_username() if run.requested_by else "",
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "active": run.is_active,
    }


def state_json(state):
    return {
        "enabled": settings.LITEBOOKS_UPDATES_ENABLED,
        "repo": settings.LITEBOOKS_UPDATE_REPO,
        "branch": settings.LITEBOOKS_UPDATE_BRANCH,
        "last_checked_at": state.last_checked_at.isoformat() if state.last_checked_at else None,
        "check_error": state.check_error,
        "latest_sha": state.latest_sha,
        "latest_short_sha": short_sha(state.latest_sha),
        "latest_message": state.latest_message,
        "latest_committed_at": state.latest_committed_at.isoformat() if state.latest_committed_at else None,
        "commits_behind": state.commits_behind,
        "commit_log": state.commit_log,
        "image_available": state.image_available,
        "update_available": update_available(state),
    }
