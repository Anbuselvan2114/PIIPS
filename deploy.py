"""
Publish orchestration for the Super-Admin "Publish" feature.

Runs ON the machine that hosts this backend (today: the dev machine), and
reaches OUT to the registered UAT/Live server over its admin share + remote
service control - the same robocopy + sc.exe steps documented in
Deploy_To_Network.txt, just triggered from the app instead of typed by hand.
That machine's account (whatever runs the PIIPS_Backend Windows service)
must already have admin rights on the target servers, and git/npm must be
on its PATH - this module assumes both, it does not set either up.

Publishing to UAT first commits+pushes whatever is currently sitting
uncommitted in this working copy onto 'Development' (a safety net so local
edits are never silently dropped by branch switches), then merges
'Development' into 'uat' - so Publish-to-UAT always deploys the very latest
local edits, not just whatever 'uat' already had.

Publishing to Live does NOT touch Development or re-merge anything - it is
a pure promotion of whatever 'uat' already has, deployed as-is. This means
new work always lands on UAT first; Live only ever gets what's already been
promoted into 'uat'.

'live' currently publishes from the 'uat' git branch itself (the 'live'
branch has no app code merged into it yet) - update BRANCH_FOR_ENV once
real releases start landing on 'live'.
"""

import os
import subprocess
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BRANCH_FOR_ENV = {"uat": "uat", "live": "uat"}

_ROBOCOPY_XD = [
    ".git", ".claude", "venv", ".venv", "__pycache__", "node_modules",
    "logs", "output", "New_Format", "model_backups", "old",
    "sample_input", "manuals",
]
_ROBOCOPY_XF = ["config.json", "config_nonencrypted.json", "*.log", "*.pyc"]


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
    dropped by the branch switch that follows. A clean tree or a failed push
    (no network, etc.) are both non-fatal - the commit (if any) still exists
    locally either way, so the checkout below can proceed regardless."""
    _run(["git", "checkout", "Development"], BASE_DIR, log)

    status = _run(["git", "status", "--porcelain"], BASE_DIR, log)
    if not status.stdout.strip():
        log.append("No local changes to push.")
        return

    _run(["git", "add", "-A"], BASE_DIR, log)
    _run(["git", "commit", "-m", "Auto-publish: sync local changes"], BASE_DIR, log)
    try:
        _run(["git", "push", "origin", "Development"], BASE_DIR, log)
    except PublishError:
        log.append("[WARNING] git push to Development failed - commit is local-only for now.")


def _merge_development_into_uat(log):
    """Fast-forward/merge 'Development' into 'uat' so a Publish run always
    deploys the very latest local edits, not just whatever 'uat' already
    had. A merge conflict is a real stop - it means someone needs to
    resolve it by hand, so this does NOT swallow that failure. Pushing the
    merge back to origin is best-effort like the other pushes above."""
    _run(["git", "checkout", "uat"], BASE_DIR, log)
    _run(["git", "merge", "Development", "--no-edit"], BASE_DIR, log)
    try:
        _run(["git", "push", "origin", "uat"], BASE_DIR, log)
    except PublishError:
        log.append("[WARNING] git push to uat failed - merge is local-only for now.")


def publish(environment, server):
    """Push local edits to Development, pull the mapped branch, rebuild the
    frontend, copy to `server` (a dict from database.get_deploy_server) and
    restart it there.

    Returns the full log text on success; raises PublishError (whose
    message IS the log so far) on the first failed step.
    Sample: publish('uat', database.get_deploy_server('uat'))
    """
    branch = BRANCH_FOR_ENV.get(environment)
    if not branch:
        raise PublishError(f"No git branch mapped for environment '{environment}'.")

    log = [f"Publishing '{environment}' from branch '{branch}' to {server['ServerHost']}"]

    if environment == "uat":
        # UAT is where fresh local edits land: back them up onto Development,
        # then promote Development into uat, so Publish-to-UAT always
        # deploys the very latest local work.
        _push_local_changes_to_development(log)
        _merge_development_into_uat(log)
    else:
        # Live is a pure promotion of whatever uat already has - it must
        # never pull in edits Development/uat haven't been through first.
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

    robocopy_cmd = (
        ["robocopy", BASE_DIR, server["FolderPath"], "/E", "/XJ",
         "/R:2", "/W:5", "/MT:8", "/XD", *_ROBOCOPY_XD, "/XF", *_ROBOCOPY_XF]
    )
    # robocopy's own exit codes: 0-7 = success (bit flags for what changed),
    # 8+ = real failure. Not a normal 0-only process exit code.
    _run(robocopy_cmd, BASE_DIR, log, allow_returncodes=range(0, 8))

    host = server["ServerHost"]
    service = server["ServiceName"] or "PIIPS_Backend"
    _run(["sc.exe", f"\\\\{host}", "stop", service], BASE_DIR, log, allow_returncodes=(0, 1062))
    time.sleep(3)
    _run(["sc.exe", f"\\\\{host}", "start", service], BASE_DIR, log)

    port = server.get("Port")
    if port:
        log.append("Waiting for the service to come back up...")
        url = f"http://{host}:{port}/health"
        ok = False
        for _ in range(10):
            time.sleep(3)
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if resp.status == 200:
                        log.append(f"Health check OK: {url}")
                        ok = True
                        break
            except Exception as exc:  # noqa: BLE001
                log.append(f"  ({exc})")
        if not ok:
            log.append(f"[WARNING] {url} did not respond healthy in time - verify manually.")

    log.append("Publish complete.")
    return "\n".join(log)
