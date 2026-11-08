"""Editing factors must not rewrite already published assessment messages."""
from copy import deepcopy

import pytest

from models import ChatSession
from routers.chat import get_session
from routers.customers import update_customer_factors
from schemas import FactorUpdateRequest
from services.ai.orchestrator import execute
from test_ai_protocol import Adapter, v2db


@pytest.mark.parametrize("new_session", [False, True], ids=["same-session", "new-session"])
def test_saved_factors_only_affect_new_assessments(v2db, new_session):
    db, customer, original_session = v2db
    customer_id, original_id = customer.id, original_session.id
    update_customer_factors(customer_id, FactorUpdateRequest(factors={"kcr_01": "0-39%"}), db)
    first = execute(db, customer_id, "assessment", session_id=original_id,
                    adapter=Adapter(), retrieve_evidence=False)
    assert first.status == "succeeded", first.error_code
    frozen_result = deepcopy(first.validated_result)
    frozen_package = deepcopy(first.fact_package)
    original_message = get_session(original_id, db).messages[0].model_dump(mode="json")

    saved = update_customer_factors(customer_id, FactorUpdateRequest(factors={"kcr_01": "100%"}), db)
    assert saved.assessment.total_score > frozen_result["assessment"]["total_score"]
    assert get_session(original_id, db).messages[0].model_dump(mode="json") == original_message

    target_id = original_id
    if new_session:
        target = ChatSession(customer_id=customer_id, scenario="assessment")
        db.add(target)
        db.commit()
        target_id = target.id
    latest = execute(db, customer_id, "assessment", session_id=target_id,
                     adapter=Adapter(), retrieve_evidence=False)
    assert latest.status == "succeeded", latest.error_code
    latest_id = latest.run_id
    db.expire_all()
    current_messages = get_session(target_id, db).messages
    new_message = next(message for message in current_messages if message.run_id == latest_id)
    assert new_message.assessment.total_score == saved.assessment.total_score
    assert latest.fact_package["raw"]["custom_fields"]["kcr_01"] == "100%"
    assert get_session(original_id, db).messages[0].model_dump(mode="json") == original_message
    assert first.validated_result == frozen_result
    assert first.fact_package == frozen_package
