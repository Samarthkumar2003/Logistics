"""
Every endpoint is behind a bearer token, and a rejection is readable.

Closes BUGS.md P1-1. The tests worth having here are not "does bcrypt work" —
they are the three ways this kind of middleware is usually broken:

    1. The 401 is unreadable by the browser. Auth registered outside CORS means
       the rejection carries no Access-Control-Allow-Origin, `fetch` rejects with
       a generic TypeError, and the frontend cannot tell an expired session from a
       dead server — so it never redirects to /login.
    2. The 401 is not JSON. AppException raised inside a BaseHTTPMiddleware
       bypasses its handler and the caller gets `Internal Server Error` as text.
    3. The failure message distinguishes "no such user" from "wrong password",
       turning /auth/login into an account-existence oracle for the company domain.

Offline like the rest of the suite: user_repo is stubbed, nothing opens a socket.
"""

import time
from dataclasses import replace

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.core import security
from backend.core.config import settings
from backend.domain.models import AppUser
from backend.services import auth_service

PASSWORD = "correct-horse-battery"
OTHER_ORIGIN = "https://evil.example.com"


@pytest.fixture
def user() -> AppUser:
    return AppUser(
        id="11111111-1111-1111-1111-111111111111",
        email="ops@example.com",
        password_hash=security.hash_password(PASSWORD),
        full_name="Ops Person",
        role="operator",
        is_active=True,
    )


@pytest.fixture
def stub_users(monkeypatch, user):
    """Put one operator in the "database". Returns a dict the test can mutate."""
    store = {"user": user}

    def get_by_email(email: str):
        current = store["user"]
        return current if current and current.email == email.strip().lower() else None

    def get_by_id(user_id: str):
        current = store["user"]
        # Mirrors the real repo, which selects everything except password_hash.
        return replace(current, password_hash="") if current and current.id == user_id else None

    monkeypatch.setattr("backend.repositories.user_repo.get_by_email", get_by_email)
    monkeypatch.setattr("backend.repositories.user_repo.get_by_id", get_by_id)
    monkeypatch.setattr("backend.repositories.user_repo.touch_last_login",
                        lambda _id: None)
    # login() calls this for the timing-equalising verify. lru_cache means the
    # bcrypt cost is paid once for the whole file.
    auth_service._dummy_hash.cache_clear()
    return store


@pytest.fixture
def app(monkeypatch):
    """The real app with auth forced ON, regardless of AUTH_ENABLED.

    Same containment as tests/test_error_logging.py: stub default_log_file before
    api.py is first imported, or importing it starts a rotating file handler and
    creates logs/ mid-test-run.
    """
    monkeypatch.setattr("backend.core.logging_config.default_log_file", lambda: None)

    from backend.app.api import create_app

    app = create_app(auth_enabled=True)

    @app.get("/__test_protected")
    def protected():
        return {"ok": True}

    return app


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _token_for(user: AppUser) -> str:
    token, _ = security.mint_token(user.id, user.email, user.role)
    return token


# --- 1. the check applies, and to everything --------------------------------

def test_protected_route_without_a_token_is_401(client):
    response = client.get("/__test_protected")
    assert response.status_code == 401


def test_protected_route_with_a_valid_token_passes_through(client, user):
    response = client.get("/__test_protected",
                          headers={"Authorization": f"Bearer {_token_for(user)}"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("header, why", [
    ("", "empty header"),
    ("Bearer", "scheme with no token"),
    ("Bearer ", "scheme with whitespace only"),
    ("Basic dXNlcjpwYXNz", "wrong scheme"),
    ("Bearer not.a.jwt", "malformed token"),
])
def test_hostile_authorization_headers_are_401(client, header, why):
    response = client.get("/__test_protected", headers={"Authorization": header})
    assert response.status_code == 401, why


def test_lowercase_bearer_scheme_is_accepted(client, user):
    """RFC 7235 says the scheme is case-insensitive, and some HTTP clients send
    `bearer`. Rejecting it would be a bug that only shows up outside the browser."""
    response = client.get("/__test_protected",
                          headers={"Authorization": f"bearer {_token_for(user)}"})
    assert response.status_code == 200


def test_expired_token_is_401(client, user):
    expired = jwt.encode(
        {"sub": user.id, "email": user.email, "role": user.role,
         "exp": int(time.time()) - 60},
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )
    response = client.get("/__test_protected",
                          headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_token_signed_with_another_key_is_401(client, user):
    """The whole security property in one test: possession of a well-formed token
    is worth nothing without JWT_SECRET."""
    forged = jwt.encode(
        {"sub": user.id, "exp": int(time.time()) + 3600},
        "a-different-key-entirely-not-the-real-one", algorithm="HS256",
    )
    response = client.get("/__test_protected",
                          headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_alg_none_token_is_rejected(client, user):
    """`decode_token` pins algorithms instead of trusting the token header. An
    unsigned token that the library was willing to accept would make every other
    test in this file meaningless."""
    unsigned = jwt.encode({"sub": user.id, "exp": int(time.time()) + 3600},
                          key="", algorithm="none")
    response = client.get("/__test_protected",
                          headers={"Authorization": f"Bearer {unsigned}"})
    assert response.status_code == 401


# --- 2. the rejection is usable ---------------------------------------------

def test_401_body_is_json_with_a_detail(client):
    """Regression for the middleware-layering trap. `@app.middleware("http")` runs
    outside the ExceptionMiddleware that AppException's handler is registered on,
    so raising it here would produce a 500 with the text body `Internal Server
    Error` — and the frontend reads `.detail` on every failure."""
    response = client.get("/__test_protected")

    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]


def test_401_carries_cors_headers_so_the_browser_can_read_it(client):
    """The reason CORSMiddleware is registered LAST in create_app().

    Without the header the browser discards the response before JavaScript sees
    it: `fetch` rejects, the frontend's 401 branch never runs, and an expired
    session shows up as "Failed to fetch" with no redirect to /login.
    """
    origin = settings.cors_origins[0]
    response = client.get("/__test_protected", headers={"Origin": origin})

    assert response.status_code == 401
    assert response.headers.get("access-control-allow-origin") == origin


def test_401_still_gets_a_request_id(client):
    """correlate is registered outside auth, so a rejected request is still
    traceable — "who is hitting this with a bad token" is the question a 401
    raises."""
    response = client.get("/__test_protected")
    assert response.headers.get("X-Request-ID")


def test_preflight_is_not_blocked_by_auth(client):
    """A preflight OPTIONS carries no Authorization header by definition. If auth
    sat outside CORS it would 401 here and every cross-origin call would fail
    before the real request was ever sent."""
    response = client.options(
        "/__test_protected",
        headers={"Origin": settings.cors_origins[0],
                 "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin")


def test_unlisted_origin_is_not_granted_cors(client):
    response = client.get("/__test_protected", headers={"Origin": OTHER_ORIGIN})
    assert response.headers.get("access-control-allow-origin") != OTHER_ORIGIN


# --- 3. public paths ---------------------------------------------------------

def test_health_needs_no_token(client):
    """Railway's healthcheck cannot send an Authorization header; a 401 there
    fails the deploy and rolls it back.

    Not asserting 200: /health talks to Supabase and the suite blocks sockets, so
    it legitimately fails here. The only thing under test is that it is not
    *refused* before it gets that far.
    """
    assert client.get("/health").status_code != 401


def test_login_needs_no_token(client, stub_users):
    response = client.post("/auth/login",
                           json={"email": "ops@example.com", "password": PASSWORD})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/metrics", "/jobs", "/agents", "/docs",
                                  "/openapi.json"])
def test_everything_else_is_closed(client, path):
    """Explicitly including /metrics and the schema endpoints. /metrics reports the
    desk's live volumes; /openapi.json is the full request shape of /send-rfq,
    which mails real freight agents."""
    assert client.get(path).status_code == 401


# --- 4. login rules ----------------------------------------------------------

def test_login_returns_a_token_and_the_user_without_the_hash(client, stub_users):
    body = client.post("/auth/login",
                       json={"email": "ops@example.com",
                             "password": PASSWORD}).json()

    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["email"] == "ops@example.com"
    assert "password_hash" not in body["user"], (
        "AppUser carries the bcrypt hash; the response model must not"
    )


def test_login_email_is_case_insensitive(client, stub_users):
    response = client.post("/auth/login",
                           json={"email": "  OPS@Example.COM  ",
                                 "password": PASSWORD})
    assert response.status_code == 200


@pytest.mark.parametrize("email, password, why", [
    ("ops@example.com", "wrong-password", "known user, wrong password"),
    ("nobody@example.com", PASSWORD, "unknown user"),
    ("nobody@example.com", "wrong-password", "unknown user, wrong password"),
])
def test_every_failed_login_gives_the_same_message(client, stub_users,
                                                   email, password, why):
    """No account-existence oracle. Usernames here are work email addresses on one
    corporate domain, so a distinguishable "no such user" enumerates the staff."""
    response = client.post("/auth/login", json={"email": email, "password": password})

    assert response.status_code == 401, why
    assert response.json()["detail"] == auth_service.INVALID_CREDENTIALS, why


def test_deactivated_account_cannot_log_in_and_is_not_identifiable(client,
                                                                   stub_users, user):
    stub_users["user"] = replace(user, is_active=False)

    response = client.post("/auth/login",
                           json={"email": "ops@example.com", "password": PASSWORD})

    assert response.status_code == 401
    assert response.json()["detail"] == auth_service.INVALID_CREDENTIALS, (
        "'account disabled' confirms the address is real"
    )


def test_login_with_a_missing_field_is_422_not_500(client, stub_users):
    assert client.post("/auth/login", json={"email": "ops@example.com"}).status_code == 422


# --- 5. /auth/me re-reads the operator --------------------------------------

def test_me_returns_the_signed_in_operator(client, stub_users, user):
    body = client.get("/auth/me",
                      headers={"Authorization": f"Bearer {_token_for(user)}"}).json()
    assert body["email"] == "ops@example.com"
    assert body["role"] == "operator"


def test_me_rejects_a_token_whose_user_was_deactivated(client, stub_users, user):
    """The middleware is signature-only for speed, so a deactivated operator's
    existing token still passes it. /auth/me is the re-read that catches them, and
    the frontend calls it on load — so the lag is one page load, not
    JWT_EXPIRY_MINUTES."""
    token = _token_for(user)
    stub_users["user"] = replace(user, is_active=False)

    assert client.get("/auth/me",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_me_rejects_a_token_for_a_deleted_user(client, stub_users, user):
    token = _token_for(user)
    stub_users["user"] = None

    assert client.get("/auth/me",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401


# --- 6. password hashing ----------------------------------------------------

def test_hash_round_trips_and_rejects_the_wrong_password():
    hashed = security.hash_password(PASSWORD)
    assert security.verify_password(PASSWORD, hashed)
    assert not security.verify_password(PASSWORD + "x", hashed)


def test_two_hashes_of_one_password_differ():
    """Per-hash salt. Identical hashes would mean a leaked table shows which
    operators share a password."""
    assert security.hash_password(PASSWORD) != security.hash_password(PASSWORD)


def test_password_over_bcrypts_72_byte_limit_is_rejected_not_truncated():
    """bcrypt ignores everything past 72 bytes silently, so a password manager's
    128-char output would be stored as its first 72 — and any other password
    sharing that prefix would verify."""
    with pytest.raises(ValueError, match="72"):
        security.hash_password("A" * 73)


def test_verify_does_not_apply_the_length_limit():
    """Only hashing rejects long input. Enforcing it on verify too would lock out
    anyone whose stored hash predates the check."""
    hashed = security.hash_password("A" * 72)
    assert security.verify_password("A" * 72, hashed)


def test_short_password_is_rejected():
    with pytest.raises(ValueError, match="at least"):
        security.hash_password("short")


def test_unusable_stored_hash_is_false_not_an_exception():
    """A hand-seeded or NULL password_hash must read as a failed login, not as a
    500 that looks like the API is down."""
    assert security.verify_password(PASSWORD, "") is False
    assert security.verify_password(PASSWORD, "not-a-bcrypt-hash") is False


# --- 7. auth off ------------------------------------------------------------

def test_auth_can_be_disabled_for_the_offline_suite(monkeypatch):
    """The escape hatch the other twenty test files rely on. Worth a test of its
    own so nobody 'fixes' create_app by removing the parameter."""
    monkeypatch.setattr("backend.core.logging_config.default_log_file", lambda: None)
    from backend.app.api import create_app

    open_app = create_app(auth_enabled=False)

    @open_app.get("/__test_open")
    def _open():
        return {"ok": True}

    # No `with`: entering TestClient as a context manager runs the lifespan, which
    # starts the scheduler and its ten jobs inside a unit test.
    c = TestClient(open_app, raise_server_exceptions=False)
    assert c.get("/__test_open").status_code == 200
