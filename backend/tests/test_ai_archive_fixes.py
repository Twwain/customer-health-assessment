"""Published chat messages retain their own scoring snapshots."""
from copy import deepcopy

import pytest

from models import ChatMessage
from schemas import ChatMessageItem
from services.ai import repository
from services.ai.orchestrator import execute
from test_ai_protocol import Adapter, v2db


def test_display_messages_use_published_snapshot_without_model_or_preview_expansion(v2db):
    db, customer, session = v2db
    run = execute(db, customer.id, "assessment", session_id=session.id,
                  adapter=Adapter(), retrieve_evidence=False)
    saved = deepcopy(run.validated_result)
    message = db.query(ChatMessage).one()
    customer.custom_fields = {"kcr_01": "100%"}
    db.commit()
    display = repository.resolve_messages(db, [message])[0]
    assert display["assessment"] == saved["assessment"]
    assert display["trend"] == saved["trend"]
    assert ChatMessageItem.model_validate(display).assessment.total_score == saved["assessment"]["total_score"]
    assert "fact_package" not in display
    for purpose in ("model_history", "preview"):
        view = repository.resolve_messages(db, [message], purpose=purpose)[0]
        assert "assessment" not in view and "trend" not in view


@pytest.mark.parametrize("state", ["legacy", "missing_run", "unavailable", "old_fields", "invalid_fields"])
def test_unavailable_or_old_messages_have_no_frozen_card(v2db, state):
    db, customer, session = v2db
    run = execute(db, customer.id, "assessment", session_id=session.id,
                  adapter=Adapter(), retrieve_evidence=False)
    message = db.query(ChatMessage).one()
    if state == "legacy":
        message.content_format = "legacy"
    elif state == "missing_run":
        db.delete(run)
    elif state == "unavailable":
        run.status = "source_unavailable"
    else:
        result = deepcopy(run.validated_result)
        for key in ("assessment", "trend"):
            result.pop(key)
            if state == "invalid_fields":
                result[key] = {"private": "not a display snapshot"}
        run.validated_result = result
    db.commit()
    display = repository.resolve_messages(db, [message])[0]
    assert display["assessment"] is None and display["trend"] is None
    ChatMessageItem.model_validate(display)
