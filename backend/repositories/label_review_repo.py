"""
label_review_repo.py
--------------------
Human label reviews: one row per operator judgement on a classifier label.

Supersedes the writes to `classification_feedback`, which copied up to 5000
characters of customer email body into every row, kept no id for the email it
judged, and had no constraint separating confirmations from corrections.
Nothing ever read it, and nothing usefully could: with only a duplicated
subject to match on, a row could not be joined back to the email it was about,
so classifier accuracy was unanswerable from the one table built to answer it.
That table is left in place with its single historical row, simply no longer
written.

`email_id` is the provider message id, the same value `email_classifications`
is keyed on and `emails.provider_msg_id` holds, so a review joins to both.

Confirmations are recorded alongside corrections; `predicted_label ==
corrected_label` means the operator agreed. Both halves are needed, because
corrections on their own are a numerator with no denominator, and accuracy is
the number this table exists to produce.

Deliberately not deduplicated. A second review of the same email is signal
rather than noise: it means the categories were ambiguous or the operator
changed their mind. The authoritative current label lives in
`email_classifications`, which leaves this table free to be an append-only log.
"""

import logging

from backend.core.db import get_db

logger = logging.getLogger(__name__)

TABLE = "label_reviews"


def record(
    email_id: str,
    predicted_label: str,
    corrected_label: str,
    confidence: float = 0.0,
    reviewed_by: str = "",
) -> None:
    """Append one review. Raises on failure instead of swallowing it.

    The exception is left to propagate because the caller is a route that has
    to be able to tell the operator their judgement was not stored. Returning
    success after a failed write is the exact defect this replaces: the old
    `submit_feedback` caught everything and returned `{"status": "error"}` with
    an HTTP 200, which the browser read as `res.ok` and rendered as saved.

    Choosing the status code is the route's decision, not this module's.
    """
    get_db().table(TABLE).insert({
        "email_id": email_id,
        "predicted_label": predicted_label,
        "corrected_label": corrected_label,
        "confidence": confidence,
        "reviewed_by": reviewed_by,
    }).execute()
