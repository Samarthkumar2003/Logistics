"""Login, whoami, and password rotation.

The only router with a public endpoint. `/auth/login` is exempt from the bearer
check in app/auth.py; the other two are not, so they read the token the
middleware already validated.
"""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from backend.app.errors import AppException
from backend.core import security
from backend.services import auth_service
from backend.services.auth_service import AuthError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # `str`, not pydantic's EmailStr: that type needs the `email-validator`
    # package, and a whole dependency to reject `sam@` on a form whose only
    # correct answer is a known address in app_users is not worth it.
    # auth_service.login strips and lowercases; an unparseable address simply
    # fails the lookup like any other wrong email.
    email: str = Field(min_length=3, max_length=320)
    # No max_length matching bcrypt's 72-byte ceiling: this is a *login*, and a
    # length rule here would reject a long password that was somehow stored
    # before that limit was enforced. hash_password owns the ceiling.
    password: str = Field(min_length=1)


class UserResponse(BaseModel):
    """What the frontend is told about the signed-in operator.

    Distinct from domain.models.AppUser precisely because that dataclass carries
    `password_hash`. Returning the dataclass directly would ship the bcrypt hash
    to the browser on every login.
    """
    id: str
    email: str
    full_name: str
    role: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # Seconds, not an absolute timestamp: the browser clock and this server's
    # clock disagree by an unknown amount, and a client-side expiry check against
    # a skewed absolute time either logs people out early or too late.
    expires_in: int
    user: UserResponse


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=security.MIN_PASSWORD_LENGTH)


def _bearer(request: Request) -> str:
    """Re-read the token off the request.

    The middleware has already validated it, so this cannot fail in practice —
    but these two routes need the raw string to hand to the service layer, and
    reaching into `request.state` for a value the service would re-decode anyway
    buys nothing.
    """
    try:
        return security.bearer_token(request.headers.get("Authorization"))
    except security.TokenError as exc:
        raise AppException(status_code=401, detail=str(exc))


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    """Exchange email + password for a bearer token.

    401 on every failure with one message — see auth_service.INVALID_CREDENTIALS
    for why the reason is never narrowed.
    """
    try:
        token, expires_in, user = auth_service.login(payload.email, payload.password)
    except AuthError as exc:
        raise AppException(status_code=401, detail=str(exc))
    except Exception as e:
        # A Supabase outage during login is a 503, not a failed password. Told
        # apart because otherwise the operator retypes a correct password until
        # somebody reads the log.
        logger.exception("Login failed for %s with an unexpected error", payload.email)
        raise AppException(status_code=503, detail=f"Login unavailable: {e}")

    return LoginResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserResponse(id=user.id, email=user.email,
                          full_name=user.full_name, role=user.role),
    )


@router.get("/me", response_model=UserResponse)
def me(request: Request):
    """Who the current token belongs to, re-read from the database.

    The frontend calls this once on load to decide whether to render the app or
    bounce to /login. It is the check that makes a deactivated account take effect
    inside one page load rather than at token expiry.
    """
    try:
        user = auth_service.resolve_user(_bearer(request))
    except AuthError as exc:
        raise AppException(status_code=401, detail=str(exc))
    except Exception as e:
        raise AppException(status_code=503, detail=f"Could not load account: {e}")

    return UserResponse(id=user.id, email=user.email,
                        full_name=user.full_name, role=user.role)


@router.post("/change-password", status_code=204)
def change_password(payload: ChangePasswordRequest, request: Request):
    """Rotate your own password. 204, so the frontend has nothing to parse.

    Any token issued before this call keeps working until it expires. That is a
    known gap, not an oversight — there is no revocation list.
    """
    try:
        auth_service.change_password(
            _bearer(request), payload.current_password, payload.new_password
        )
    except AuthError as exc:
        # 400 rather than 401: the caller IS authenticated, their input is wrong.
        # A 401 here would make the frontend's interceptor bounce them to /login
        # for a typo in the old password.
        raise AppException(status_code=400, detail=str(exc))
    except Exception as e:
        logger.exception("Password change failed")
        raise AppException(status_code=503, detail=f"Could not change password: {e}")
