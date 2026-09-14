"""
sender.py
---------
Who an outgoing vendor email is signed by.

Shared by the two routes that put a name in front of a freight agent: drafting an
RFQ (routes/rfq.py) and awarding one (routes/jobs.py). It lives in its own module
rather than in either of them because one source for "who is sending this" is the
only way the two stay consistent. An acceptance signed differently from the RFQ it
answers reads, to the agent, like it came from a different company.
"""

import logging

from fastapi import Request

from backend.app.errors import AppException
from backend.domain.models import SenderIdentity
from backend.repositories import user_repo

logger = logging.getLogger(__name__)

NO_SENDER_NAME = (
    "Your operator profile has no display name, so an RFQ cannot be signed. "
    "Set full_name on your app_users row and log in again."
)


def _company_for(claims) -> str:
    """The company line, read live from the operator's `app_users` row.

    Not from the environment, and not carried on the token. Reading the row means
    changing the company is one UPDATE that takes effect on the very next email —
    no redeploy, and without invalidating anyone's session. That is the whole
    reason it stopped being COMPANY_NAME.

    Failure degrades to an empty company rather than a refusal. `SenderIdentity`
    already renders a name-only signature, and a real operator's name is a
    perfectly good sign-off; losing a draft because one cosmetic lookup failed
    would not be. A missing row is treated the same way: deciding whether a token
    still names a real user is authentication's job, not this function's.
    """
    user_id = (getattr(claims, "user_id", "") or "").strip()
    if not user_id:
        return ""
    try:
        user = user_repo.get_by_id(user_id)
    except Exception as e:
        logger.warning("Could not read the company line for user %s: %s", user_id, e)
        return ""
    return (getattr(user, "company_name", "") or "").strip() if user else ""


def sender_or_422(request: Request) -> SenderIdentity:
    """Who this email will be signed by, or a refusal.

    The name comes from the verified token: the bearer middleware has already put
    the claims on `request.state`, so it costs nothing. Three shapes have to read
    the same way —

      * a token minted before the `name` claim existed (attribute absent);
      * a row whose `full_name` was never filled in (claim defaults to '');
      * AUTH_ENABLED=0, where the middleware returns before setting claims at all,
        so `request.state.claims` does not exist — hence getattr, not attribute
        access, or this would be a 500 instead of a 422.

    The last case means the drafting endpoints do not work with auth disabled.
    That is correct: with no token there is no operator, and an unsigned RFQ to a
    vendor is worse than a refused one. The offline suite drives rfq_service
    directly and passes its own SenderIdentity, so nothing there depends on this.

    There is deliberately no default for the name. An empty one is what produced
    the `[Your Name]` placeholders that once reached real freight agents: the
    model was asked for a professional email, given nobody to sign it as, and did
    the only thing left.
    """
    claims = getattr(request.state, "claims", None)
    name = (getattr(claims, "name", "") or "").strip()
    if not name:
        raise AppException(status_code=422, detail=NO_SENDER_NAME)
    return SenderIdentity(name=name, company=_company_for(claims))
