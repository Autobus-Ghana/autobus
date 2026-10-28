import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import logging
from another_fastapi_jwt_auth import AuthJWT
from core.auth.dependencies import validate_token, get_current_user, get_db
from core.user.model.User import User
from core.nlu.dto.reponse.nluresponse import NLUResponse
from core.nlu.nlu import get_nlu_system
from core.nlu.dto.request.nlurequest import NLURequest, NLUDetectRequest
from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.subscription.service.subscription_service import SubscriptionService

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

nlu_routes = APIRouter()


@nlu_routes.post("/process", response_model=NLUResponse)
async def process_message(
    request: NLURequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Process natural language messages (authenticated)."""
    try:
        phone = current_user.phone or request.phone
        if request.phone and current_user.phone and request.phone != current_user.phone:
            raise HTTPException(status_code=403, detail="Phone does not match authenticated user")

        subscription_service = SubscriptionService(db)
        result = subscription_service.get_user_subscription_status(current_user.id)

        credit_service = CreditService(db)
        credit_service.assert_can_spend(current_user.id, CreditType.LLM.value, 1.0)
        credit_service.check_and_deduct(
            current_user.id,
            CreditType.LLM.value,
            1.0,
            "nlu_message",
        )

        logger.info("Processing message for user %s", current_user.id)

        response = get_nlu_system().process_message(
            phone,
            request.message,
        )

        return NLUResponse(
            user_id=phone or current_user.id,
            message=request.message,
            response=response,
            success=True,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing NLU message: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing message",
        )


@nlu_routes.post("/detect")
async def detect_intent(
    request: NLUDetectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Classify intent with the NLU engine without executing handlers or mutating chat state."""
    try:
        CreditService(db).require_credits(current_user.id, CreditType.LLM.value, 1.0, "nlu_detect")
        logger.info("Detecting intent for user %s", current_user.id)
        history = []
        for item in request.conversation or []:
            role = (item.get("role") or "").strip() or "user"
            content = (item.get("content") or item.get("text") or "").strip()
            if content:
                history.append({"role": role, "content": content})

        intent, slots, missing_slots = get_nlu_system().intent_detector.detect_intent_and_slots(
            request.message,
            history,
            request.current_intent,
        )
        return {
            "intent": intent,
            "slots": slots or {},
            "missing_slots": missing_slots or [],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error detecting intent: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error detecting intent",
        )


@nlu_routes.get("/conversation-history")
async def get_conversation_history(
    current_user: User = Depends(get_current_user),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Get authenticated user's conversation history."""
    try:
        conversation_state = get_nlu_system().conversation_manager.get_conversation_state(
            current_user.phone or current_user.id
        )
        return {
            "user_id": current_user.id,
            "conversation_history": conversation_state.conversation_history,
            "current_intent": conversation_state.current_intent,
            "collected_slots": conversation_state.collected_slots,
        }
    except Exception as e:
        logger.error(f"Error fetching conversation history: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error fetching conversation history",
        )


@nlu_routes.delete("/conversation-history")
async def clear_conversation_history(
    current_user: User = Depends(get_current_user),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Clear authenticated user's conversation history."""
    try:
        get_nlu_system().conversation_manager.reset_conversation_state(
            current_user.phone or current_user.id
        )
        return {"success": True, "message": "Conversation history cleared successfully"}
    except Exception as e:
        logger.error(f"Error clearing conversation history: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error clearing conversation history",
        )


@nlu_routes.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "Autobus NLU System",
        "timestamp": f"{datetime.datetime.utcnow().isoformat()}Z",
    }
