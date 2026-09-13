"""
Human label reviews: what reaches storage, and what is refused.

The endpoint these cover replaced one that could not fail visibly. The old
`submit_feedback` caught every exception and returned `{"status": "error"}` with
an HTTP 200, which the browser read as `res.ok` and rendered as "Corrected", so
a storage outage looked identical to a successful save. It also accepted a
payload with no `email_id`, stored an audit row anyway, and skipped the cache
update, meaning the call reported success while changing no label at all.

Both of those are pinned below, because both are silent by construction and a
regression would not surface in the UI.
"""

from types import SimpleNamespace

import pytest

from backend.app.errors import AppException
from backend.app.routes import ops
from backend.app.routes.ops import FeedbackRequest, feedback_endpoint
from backend.repositories import label_review_repo


def _request(email: str | None = None):
    """A request carrying token claims, or none at all.

    `email=None` is the AUTH_ENABLED=0 shape, where the bearer middleware
    returns before setting claims, so `request.state` has no `claims` attribute.
    """
    state = SimpleNamespace()
    if email is not None:
        state.claims = SimpleNamespace(email=email)
    return SimpleNamespace(state=state)


@pytest.fixture
def recorded(monkeypatch):
    """Capture review rows instead of writing them, and the cache calls too.

    Both are captured by one fixture on purpose: several tests below assert not
    only what was stored but that the cache update did NOT run, which is the
    ordering guarantee that makes a failed review safe to retry.
    """
    rows: list[dict] = []
    cache: list[tuple] = []
    monkeypatch.setattr(label_review_repo, "record",
                        lambda **kw: rows.append(kw))
    monkeypatch.setattr(ops, "cache_update_label",
                        lambda *a: cache.append(a))
    return rows, cache


def test_a_confirmation_is_stored_not_only_corrections(recorded):
    """`✓ Approve` must leave a row. Corrections alone are a numerator with no
    denominator, so accuracy would be unstateable."""
    rows, cache = recorded
    result = feedback_endpoint(
        FeedbackRequest(email_id="19f7", predicted_label="general",
                        corrected_label="general", confidence=0.4),
        _request("op@desk.test"),
    )
    assert len(rows) == 1
    assert rows[0]["predicted_label"] == rows[0]["corrected_label"] == "general"
    assert result["agreed"] is True
    assert cache == [("19f7", "general")]


def test_a_correction_keeps_both_labels(recorded):
    rows, _ = recorded
    result = feedback_endpoint(
        FeedbackRequest(email_id="19f8", predicted_label="general",
                        corrected_label="quotation_rate_card", confidence=0.9),
        _request("op@desk.test"),
    )
    assert rows[0]["predicted_label"] == "general"
    assert rows[0]["corrected_label"] == "quotation_rate_card"
    assert rows[0]["confidence"] == 0.9
    assert result["agreed"] is False


def test_storage_failure_is_reported_and_the_label_is_left_alone(monkeypatch):
    """The regression pin. A failed write must surface as a non-2xx, and must
    not have already changed the label: a 500 has to mean "nothing happened,
    retry", or the operator cannot act on it."""
    def _boom(**_kw):
        raise RuntimeError("PostgREST said no")

    cache: list[tuple] = []
    monkeypatch.setattr(label_review_repo, "record", _boom)
    monkeypatch.setattr(ops, "cache_update_label", lambda *a: cache.append(a))

    with pytest.raises(AppException) as exc:
        feedback_endpoint(
            FeedbackRequest(email_id="19f9", predicted_label="general",
                            corrected_label="general"),
            _request("op@desk.test"),
        )
    assert exc.value.status_code == 500
    assert cache == [], "the label must not change when the review was not stored"


def test_a_review_with_no_email_id_is_refused(recorded):
    """Used to store a row and skip the cache update, so it reported success
    while changing nothing. Now it cannot be stored at all."""
    rows, cache = recorded
    for missing in ("", "   "):
        with pytest.raises(AppException) as exc:
            feedback_endpoint(
                FeedbackRequest(email_id=missing, corrected_label="general"),
                _request("op@desk.test"),
            )
        assert exc.value.status_code == 422
    assert rows == [] and cache == []


def test_an_unknown_label_is_refused_before_anything_is_written(recorded):
    rows, cache = recorded
    with pytest.raises(AppException) as exc:
        feedback_endpoint(
            FeedbackRequest(email_id="19fa", corrected_label="not_a_label"),
            _request("op@desk.test"),
        )
    assert exc.value.status_code == 422
    assert rows == [] and cache == []


def test_the_reviewer_is_taken_from_the_verified_token(recorded):
    rows, _ = recorded
    feedback_endpoint(
        FeedbackRequest(email_id="19fb", corrected_label="general"),
        _request("  dhaval@bhatiashipping.com  "),
    )
    assert rows[0]["reviewed_by"] == "dhaval@bhatiashipping.com"


def test_a_review_without_claims_is_stored_anonymously(recorded):
    """AUTH_ENABLED=0 leaves no claims on the request at all. A review is still
    worth keeping, so this records "" rather than refusing, and must not raise
    the AttributeError that plain attribute access would."""
    rows, _ = recorded
    feedback_endpoint(
        FeedbackRequest(email_id="19fc", corrected_label="general"),
        _request(None),
    )
    assert rows[0]["reviewed_by"] == ""


def test_an_absent_predicted_label_is_recorded_as_unknown(recorded):
    rows, _ = recorded
    feedback_endpoint(
        FeedbackRequest(email_id="19fd", corrected_label="general"),
        _request("op@desk.test"),
    )
    assert rows[0]["predicted_label"] == "unknown"


def test_the_request_model_cannot_carry_the_email_again():
    """Schema pin. The body copy is what made the old table unusable for
    analysis and put 5000 characters of customer mail somewhere with no
    retention policy; re-adding a field here is how it would come back."""
    assert set(FeedbackRequest.model_fields) == {
        "email_id", "predicted_label", "corrected_label", "confidence",
    }


def test_the_repository_lets_a_write_failure_propagate(monkeypatch):
    """`record` must not catch its own errors.

    The route turns the exception into a 500, so a module that swallowed would
    put the endpoint straight back to reporting success over a lost row, which
    is the behaviour it exists to replace. Covered separately because every
    other test here stubs `record` out and so cannot see this contract.
    """
    def _dead_db():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(label_review_repo, "get_db", _dead_db)
    with pytest.raises(RuntimeError, match="connection refused"):
        label_review_repo.record(email_id="19fe", predicted_label="general",
                                 corrected_label="general")
