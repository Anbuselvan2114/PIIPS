"""
Server-side authentication / authorisation for the PIIPS API.

Until now the API trusted whatever `user_id` the browser put in a request
body or query string: every role check (`_require_developer`,
`_require_not_viewer`, ...) looked that id up, but nothing proved the caller
actually WAS that user - role gating lived only in the React UI. This module
closes that:

* `/api/login` issues a signed, expiring bearer token (HMAC-SHA256, secret
  kept in config.json as `auth_secret`, generated on first use).
* `AuthMiddleware` (pure ASGI) requires a valid token on every `/api/*`
  route except a short public list, then
    - rejects inactive/unknown users,
    - makes the Viewer role read-only,
    - applies the Screen-Access (role -> menu) mapping to the
      configuration/admin routes, so a role cannot reach an admin endpoint
      just by calling it directly,
    - rejects a request whose body/query names a DIFFERENT acting user than
      the token's owner (which is what makes the existing per-endpoint
      `user_id` checks trustworthy).
* Login and forgot-password are throttled per client to slow guessing /
  mail-bombing.

The token travels in `Authorization: Bearer <token>`; direct-navigation
URLs (PDF viewer iframe, file downloads) carry it as `?access_token=`.
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import parse_qs

import config_store

TOKEN_TTL_SECONDS = 12 * 60 * 60          # a working day; re-login afterwards

# Routes reachable without a token (pre-login screens / health probes).
PUBLIC_EXACT = {
    ("GET", "/health"),
    ("GET", "/api/version"),
    ("POST", "/api/login"),
    ("POST", "/api/forgot-password"),
    # App.jsx reads these before anyone is logged in (backend-up probe,
    # DB-configured flag, sidebar mapping). None holds anything sensitive.
    ("GET", "/api/config"),
    ("GET", "/api/role-menus"),
}

# The pre-login Database Configuration bootstrap: with no DB configured
# nobody CAN log in, so /api/db-config stays open until a connection exists
# (the endpoint itself keeps enforcing Super Admin afterwards).
BOOTSTRAP_PATHS = {"/api/db-config"}

ADMIN_ROLES = {"admin", "super admin", "developer"}
FULL_ACCESS_ROLES = {"super admin", "developer"}

# (method or "*", path prefix, menu key that grants it). Super Admin /
# Developer always pass; any other role needs the menu in its own
# Screen-Access mapping (tbl_RoleMenu), so the server enforces exactly what
# the sidebar shows.
MENU_GATES = [
    ("*",      "/api/users",             "users"),
    ("POST",   "/api/config",            "configuration"),
    ("POST",   "/api/api-config",        "apiconfig"),
    ("POST",   "/api/train",             "training"),
    ("GET",    "/api/train",             "training"),
    ("DELETE", "/api/formats",           "training"),
    ("POST",   "/api/mapping",           "mapping"),
    ("POST",   "/api/fields",            "createfield"),
    ("POST",   "/api/templates",         "template"),
]

# Routes a Viewer may still POST to.
VIEWER_WRITE_OK = {"/api/change-password"}

# Body/query keys naming the ACTING user, per path. Everything not listed
# uses DEFAULT_ACTOR_KEYS. (/api/users/active's `user_id` is the TARGET;
# its actor is `modified_by`.)
DEFAULT_ACTOR_KEYS = ("user_id", "created_by", "modified_by", "started_by")
ACTOR_KEYS_BY_PATH = {
    "/api/users/active": ("modified_by",),
}

MAX_BODY_CHECK_BYTES = 1_000_000

_CACHE_SECONDS = 30
_lock = threading.Lock()
_secret_cache = None
_role_cache = {}       # uid -> (expires, role_lower, active)
_menu_cache = [0.0, {}]


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------

def _secret():
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    with _lock:
        if _secret_cache:
            return _secret_cache
        value = (config_store.load_config().get("auth_secret") or "").strip()
        if not value:
            value = secrets.token_hex(32)
            config_store.save_config({"auth_secret": value})
        _secret_cache = value.encode("utf-8")
        return _secret_cache


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(user_id, ttl=TOKEN_TTL_SECONDS):
    """Signed bearer token for `user_id`.
    Sample: issue_token(7) -> 'eyJ1aWQiOjcsImV4cCI6...'"""
    body = _b64(json.dumps({"uid": int(user_id), "exp": int(time.time()) + ttl},
                           separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token):
    """The user id a valid, unexpired token belongs to, else None.
    Sample: verify_token(issue_token(7)) -> 7"""
    try:
        body, sig = (token or "").split(".", 1)
        expected = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if int(payload["exp"]) < time.time():
            return None
        return int(payload["uid"])
    except Exception:  # noqa: BLE001 - any malformed token is simply invalid
        return None


# --------------------------------------------------------------------------
# Throttling (login / forgot-password)
# --------------------------------------------------------------------------

_attempts = {}        # key -> [timestamps]


def throttled(key, limit, window_seconds):
    """Record an attempt for `key`; True when it exceeds `limit` attempts
    inside the trailing `window_seconds`.
    Sample: throttled('login:10.0.0.5', 10, 900) -> False"""
    now = time.time()
    with _lock:
        hits = [t for t in _attempts.get(key, []) if now - t < window_seconds]
        hits.append(now)
        _attempts[key] = hits
        if len(_attempts) > 5000:               # bound memory
            for k in [k for k, v in _attempts.items() if not v or now - v[-1] > window_seconds]:
                _attempts.pop(k, None)
        return len(hits) > limit


def clear_attempts(key):
    with _lock:
        _attempts.pop(key, None)


# --------------------------------------------------------------------------
# Role / menu lookups (cached briefly - status polling hits the API often)
# --------------------------------------------------------------------------

def _role_of(user_id):
    now = time.time()
    hit = _role_cache.get(user_id)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    import database
    info = database.get_user_role(user_id)
    role = ((info or {}).get("role") or "").strip().lower()
    active = bool(info and info.get("active"))
    _role_cache[user_id] = (now + _CACHE_SECONDS, role, active)
    return role, active


def _menus_for(role):
    now = time.time()
    if _menu_cache[0] < now:
        import database
        _menu_cache[1] = {k.lower(): set(v) for k, v in database.get_role_menus().items()}
        _menu_cache[0] = now + _CACHE_SECONDS
    return _menu_cache[1].get(role, set())


def forget_user(user_id=None):
    """Drop cached role/menu data (after a role change / Screen Access save)."""
    _role_cache.pop(user_id, None) if user_id is not None else _role_cache.clear()
    _menu_cache[0] = 0.0


# --------------------------------------------------------------------------
# ASGI middleware
# --------------------------------------------------------------------------

def _menu_required(method, path):
    for m, prefix, menu in MENU_GATES:
        if (m == "*" or m == method) and (path == prefix or path.startswith(prefix + "/")):
            return menu
    return None


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def _reject(self, scope, receive, send, status, message):
        body = json.dumps({"detail": message}).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        method = scope["method"].upper()
        path = scope["path"]
        if method == "OPTIONS" or not path.startswith("/api/"):
            return await self.app(scope, receive, send)      # CORS preflight / static
        if (method, path) in PUBLIC_EXACT:
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))

        if path in BOOTSTRAP_PATHS:
            try:
                configured = bool((config_store.load_config().get("db_connection") or "").strip())
            except Exception:  # noqa: BLE001
                configured = True
            if not configured:
                return await self.app(scope, receive, send)

        token = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif query.get("access_token"):
            token = query["access_token"][0]
        uid = verify_token(token)
        if uid is None:
            return await self._reject(scope, receive, send, 401,
                                      "Your session has expired. Please sign in again.")

        try:
            role, active = _role_of(uid)
        except Exception:  # noqa: BLE001
            return await self._reject(scope, receive, send, 500, "Database error while checking access.")
        if not active:
            return await self._reject(scope, receive, send, 401,
                                      "Your account is inactive or no longer exists.")

        if role == "viewer" and method not in ("GET", "HEAD") and path not in VIEWER_WRITE_OK:
            return await self._reject(scope, receive, send, 403, "Viewers have read-only access.")

        menu = _menu_required(method, path)
        if menu and role not in FULL_ACCESS_ROLES:
            try:
                allowed = menu in _menus_for(role)
            except Exception:  # noqa: BLE001
                return await self._reject(scope, receive, send, 500, "Database error while checking access.")
            if not allowed:
                return await self._reject(scope, receive, send, 403,
                                          "Your role does not have access to this function.")

        # The acting user named in the request must be the token's owner.
        actor_keys = ACTOR_KEYS_BY_PATH.get(path, DEFAULT_ACTOR_KEYS)
        for key in actor_keys:
            for value in query.get(key, []):
                if value.strip() and value.strip() != str(uid):
                    return await self._reject(scope, receive, send, 403,
                                              "The request names a different user than the signed-in one.")

        replay = receive
        ctype = headers.get("content-type", "")
        if method in ("POST", "PUT", "PATCH", "DELETE") and "application/json" in ctype:
            chunks, total = [], 0
            while True:
                message = await receive()
                chunks.append(message)
                total += len(message.get("body", b""))
                if not message.get("more_body") or total > MAX_BODY_CHECK_BYTES:
                    break
            try:
                data = json.loads(b"".join(c.get("body", b"") for c in chunks) or b"null")
            except Exception:  # noqa: BLE001 - let the endpoint report bad JSON
                data = None
            if isinstance(data, dict):
                for key in actor_keys:
                    val = data.get(key)
                    if val not in (None, "") and str(val) != str(uid):
                        return await self._reject(scope, receive, send, 403,
                                                  "The request names a different user than the signed-in one.")
            pending = list(chunks)

            async def replay():                       # hand the body on unchanged
                if pending:
                    return pending.pop(0)
                return await receive()

        scope.setdefault("state", {})["user_id"] = uid
        return await self.app(scope, replay, send)
