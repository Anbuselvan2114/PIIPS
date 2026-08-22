"""
Publish orchestration for the Super-Admin "Publish" feature.

Runs ON the machine that hosts this backend (today: the dev machine). It
pulls/merges the right git branch, rebuilds the frontend, and copies the
result into a LOCAL staging folder under PUBLISHED_ROOT (one subfolder per
environment) - it does NOT touch any remote server or restart any service.
Moving a staged build onto the actual UAT/Live server, and restarting it
there, is a manual step for now (see Deploy_To_Network.txt for that
runbook) - this used to also do that part automatically over the target's
admin share, but that requires the PIIPS_Backend service account to be a
local admin on the target machine, which isn't set up everywhere yet.

Publishing to UAT commits+pushes whatever is currently sitting uncommitted
in this working copy onto 'Development' (a safety net so local edits are
never silently dropped by branch switches), pulls 'Development', then
builds and copies straight from there - so Publish-to-UAT always deploys
the very latest local work. It does NOT merge anything into the 'uat' git
branch anymore - that branch is only touched by whoever/whatever promotes
to Live, separately from this.

Publishing to Live does NOT touch Development at all - it is a pure
promotion of whatever the 'uat' git branch already has, deployed as-is.
Since Publish-to-UAT no longer updates that branch, something else (a
manual merge, or a future promote step) has to move Development's work
into 'uat' before Live will ever see it.

'live' currently publishes from the 'uat' git branch itself (the 'live'
branch has no app code merged into it yet) - update BRANCH_FOR_ENV once
real releases start landing on 'live'.
"""

import os
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PUBLISHED_ROOT = r"D:\WorkSpace\Projects\Python\PIIPS_Published"

BRANCH_FOR_ENV = {"uat": "Development", "live": "uat"}

_ROBOCOPY_XD = [
    ".git", ".claude", "venv", ".venv", "__pycache__", "node_modules",
    "logs", "output", "New_Format", "model_backups", "old",
    "sample_input", "manuals", "announcement_media",
]
_ROBOCOPY_XF = ["config.json", "PIIPS.sql", "*.log", "*.pyc"]


class PublishError(Exception):
    """Raised with the accumulated log text when a publish step fails."""


def _run(cmd, cwd, log, allow_returncodes=(0,)):
    """Run one step, append its output to `log`, raise PublishError (with
    everything logged so far, not just this step) if the command can't even
    be started or its return code isn't in `allow_returncodes`."""
    log.append(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, shell=False, timeout=900,
        )
    except OSError as exc:
        log.append(f"[FAILED] {exc}")
        raise PublishError("\n".join(log)) from exc
    if result.stdout:
        log.append(result.stdout.strip())
    if result.stderr:
        log.append(result.stderr.strip())
    if result.returncode not in allow_returncodes:
        log.append(f"[FAILED] exit code {result.returncode}")
        raise PublishError("\n".join(log))
    return result


def _push_local_changes_to_development(log):
    """Commit whatever's currently sitting uncommitted in this working copy
    onto 'Development' and push it, so nothing typed here is ever silently
    dropped by the branch switch that follows. Also pushes any commits that
    already exist locally but never made it to origin (e.g. a prior push
    that failed for lack of network) - a clean working tree does NOT mean
    there's nothing to push. A failed push is non-fatal either way - the
    commit(s) still exist locally, so the checkout below can proceed."""
    _run(["git", "checkout", "Development"], BASE_DIR, log)

    status = _run(["git", "status", "--porcelain"], BASE_DIR, log)
    if status.stdout.strip():
        _run(["git", "add", "-A"], BASE_DIR, log)
        _run(["git", "commit", "-m", "Auto-publish: sync local changes"], BASE_DIR, log)
    else:
        log.append("No uncommitted local changes.")

    try:
        _run(["git", "push", "origin", "Development"], BASE_DIR, log)
    except PublishError:
        log.append("[WARNING] git push to Development failed - commit(s) are local-only for now.")


def publish(environment, published_root=None):
    """Push local edits to Development, pull the mapped branch, rebuild the
    frontend, and copy the result into <published_root>/<environment> on
    this machine (published_root defaults to DEFAULT_PUBLISHED_ROOT). Does
    not touch any remote server or service.

    Returns the full log text on success; raises PublishError (whose
    message IS the log so far) on the first failed step.
    Sample: publish('uat', r'D:\\PIIPS_Published')
    """
    branch = BRANCH_FOR_ENV.get(environment)
    if not branch:
        raise PublishError(f"No git branch mapped for environment '{environment}'.")

    dest = os.path.join(published_root or DEFAULT_PUBLISHED_ROOT, environment)
    log = [f"Publishing '{environment}' from branch '{branch}' to {dest} (local only)"]

    if environment == "uat":
        # UAT builds straight from Development: back up whatever's sitting
        # uncommitted here, then the checkout+pull below (branch=
        # "Development") picks it right back up.
        _push_local_changes_to_development(log)
    else:
        # Live is a pure promotion of whatever the uat branch already has -
        # it must never pull in edits Development hasn't been promoted
        # through first.
        log.append("Live publish does not touch Development - deploying uat as-is.")

    _run(["git", "checkout", branch], BASE_DIR, log)
    # A failed pull (no network to GitHub, etc.) is a warning, not a hard
    # stop - whatever's already on disk in this folder (already-committed
    # history plus any uncommitted edits) still gets built and copied below.
    # Skipping this only risks publishing something slightly behind origin,
    # never something broken.
    try:
        _run(["git", "pull", "origin", branch], BASE_DIR, log)
    except PublishError:
        # _run already appended the failed command's own output/exit code
        # to `log` above; just note that we're pressing on regardless.
        log.append("[WARNING] git pull failed - continuing with what's on disk.")

    # On Windows, npm ships as npm.cmd - subprocess with shell=False resolves
    # bare "npm" via CreateProcess, which (unlike cmd.exe) does not try
    # PATHEXT extensions, so it fails with WinError 2 even though npm.cmd is
    # right there on PATH.
    frontend_dir = os.path.join(BASE_DIR, "frontend")
    _run(["npm.cmd", "install"], frontend_dir, log)
    _run(["npm.cmd", "run", "build"], frontend_dir, log)

    os.makedirs(dest, exist_ok=True)
    robocopy_cmd = (
        ["robocopy", BASE_DIR, dest, "/E", "/XJ",
         "/R:2", "/W:5", "/MT:8", "/XD", *_ROBOCOPY_XD, "/XF", *_ROBOCOPY_XF]
    )
    # robocopy's own exit codes: 0-7 = success (bit flags for what changed),
    # 8+ = real failure. Not a normal 0-only process exit code.
    _run(robocopy_cmd, BASE_DIR, log, allow_returncodes=range(0, 8))

    log.append(f"Publish complete - staged at {dest}. Move it to the real server by hand.")
    return "\n".join(log)
