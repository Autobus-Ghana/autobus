from fastapi import APIRouter, Depends, HTTPException
from another_fastapi_jwt_auth import AuthJWT
from sqlalchemy.orm import Session
from core.agent.dto.commandreqeust import CommandRequest
from core.auth.dependencies import validate_token, get_current_user, get_db
from core.user.model.User import User
from core.agent.dto.media_generation_request import MediaGenerationRequest
from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.media.controller.media_controller import generate_image, generate_video
from core.media.dto.media_generation_response import ImageGenerationResponse, VideoGenerationResponse
from core.nlu.config import SYSTEM_PROMPTS
from core.nlu.nlu import get_nlu_system
from utilities.plain_text import strip_markdown_formatting
from typing import Any, Dict, Optional
from pydantic import BaseModel
from core.admin.model.automation_request import AutomationRequest
from core.auth.dependencies import resolve_user_from_jwt
import logging

logger = logging.getLogger(__name__)

MARKETING_AGENT_NAMES = frozenset({
    "marketing",
    "digital_marketing",
    "digital-marketing",
    "digital_margeting",
})

agent_routes = APIRouter()


@agent_routes.post("/command")
def agent(
    query: CommandRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    """Authenticated command. Marketing copy uses a single LLM call; everything else uses NLU."""
    allowed_ids = {x for x in (current_user.id, current_user.phone, current_user.email) if x}
    if query.userid not in allowed_ids:
        raise HTTPException(status_code=403, detail="Cannot invoke agent as another user")

    credit_service = CreditService(db)
    credit_service.require_credits(current_user.id, CreditType.LLM.value, 1.0, "agent_command")

    if _is_marketing_agent(query.agent_name):
        return {"response": _generate_marketing_content(query.message)}

    response_text = get_nlu_system().process_message(query.userid, query.message)
    return {"response": response_text}


@agent_routes.post("/generate-image", response_model=ImageGenerationResponse)
async def agent_generate_image(
    req: MediaGenerationRequest,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    return await generate_image(req, db=db, authjwt=authjwt)


@agent_routes.post("/generate-video", response_model=VideoGenerationResponse)
async def agent_generate_video(
    req: MediaGenerationRequest,
    store: bool = False,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(validate_token),
):
    return await generate_video(req, store=store, db=db, authjwt=authjwt)


def _is_marketing_agent(agent_name: str) -> bool:
    return (agent_name or "").strip().lower() in MARKETING_AGENT_NAMES


def _generate_marketing_content(prompt: str) -> str:
    from core.nlu.service.llmclient import LLMClient

    user_message = (prompt or "").strip()
    if not user_message:
        return "Please provide a short description of what you want to promote."

    response = LLMClient().chat_completion(
        system_prompt=SYSTEM_PROMPTS["marketing"],
        user_message=user_message,
        conversation_history=None,
        temperature=0.8,
        max_tokens=800,
    )
    if not response:
        return "Sorry, I could not generate marketing text right now. Please try again."
    return strip_markdown_formatting(response)


# ---------------------------------------------------------------------------
# Custom Agent Request & Questionnaire Endpoints (Autobus Portal Integration)
# ---------------------------------------------------------------------------

class CustomAgentSubmitRequest(BaseModel):
    answers: Dict[str, Any]


@agent_routes.get("/custom-request/questionnaire")
def get_custom_agent_questionnaire():
    """Returns the questionnaire configuration for the Request Custom Agent portal survey."""
    return {
        "questions": [
            {
                "id": "business_name",
                "prompt": "What is your business name?",
                "hint": "The name the custom agent should use when it talks to customers.",
                "placeholder": "e.g. Sunrise Bakery",
                "required": True,
            },
            {
                "id": "industry",
                "prompt": "What industry are you in?",
                "hint": "This helps us pick the right workflows, tone, and examples.",
                "placeholder": "Select an industry",
                "required": True,
                "options": [
                    "Agriculture & Farming",
                    "Automotive",
                    "Beauty & Personal Care",
                    "Construction & Trades",
                    "Education & Training",
                    "Fashion & Apparel",
                    "Finance & Insurance",
                    "Food & Beverage",
                    "Healthcare & Wellness",
                    "Hospitality & Tourism",
                    "Logistics & Transportation",
                    "Manufacturing",
                    "Professional Services",
                    "Real Estate & Property",
                    "Retail & E-commerce",
                    "Technology & Software",
                    "Other",
                ],
            },
            {
                "id": "business_summary",
                "prompt": "What does your business sell, and who do you serve?",
                "hint": "A short picture of the offer and the customer. The agent will use this as its starting brief.",
                "placeholder": "e.g. We sell ready-to-wear clothing to young professionals in Accra, mostly via Instagram and WhatsApp.",
                "multiline": True,
                "required": True,
            },
            {
                "id": "agent_goal",
                "prompt": "What should this custom agent mainly do?",
                "hint": "Pick the job that would save the most time if it ran well every day.",
                "placeholder": "Select a primary job",
                "required": True,
                "options": [
                    "Sell products and take orders",
                    "Answer customer questions and handle support",
                    "Book appointments or reservations",
                    "Follow up leads and recover abandoned checkouts",
                    "Help staff with internal tasks",
                    "A mix of sales and support",
                    "Something else — I will describe it below",
                ],
            },
            {
                "id": "channels",
                "prompt": "Where should the agent work?",
                "hint": "Select every channel you want in the first version.",
                "required": True,
                "allow_multiple": True,
                "options": [
                    "WhatsApp",
                    "Instagram",
                    "TikTok",
                    "Website chat",
                    "Email",
                    "SMS",
                    "Phone / voice",
                    "Internal dashboard only",
                ],
            },
            {
                "id": "languages",
                "prompt": "Which language(s) should it use?",
                "hint": "List them in the order it should prefer.",
                "placeholder": "e.g. English and Twi",
                "required": True,
            },
            {
                "id": "tone",
                "prompt": "How should the agent sound?",
                "hint": "This becomes the default voice unless a channel needs something different.",
                "placeholder": "Select a tone",
                "required": True,
                "options": [
                    "Friendly and casual",
                    "Professional and concise",
                    "Warm and sales-oriented",
                    "Playful and brand-forward",
                    "Formal",
                ],
            },
            {
                "id": "typical_requests",
                "prompt": "What are the most common requests it should handle?",
                "hint": "Price checks, size questions, delivery times, booking, complaints — list the real ones.",
                "placeholder": "e.g. Do you have this in size 42? How much is delivery to Kumasi? Can I pay on delivery?",
                "multiline": True,
                "required": True,
            },
            {
                "id": "handoff_rules",
                "prompt": "When should it pass the conversation to a person?",
                "hint": "Complaints, refunds, VIP customers, anything the agent must never guess.",
                "placeholder": "e.g. Hand over refunds, damaged-item complaints, and any order over GHS 2,000.",
                "multiline": True,
                "required": True,
            },
            {
                "id": "modules",
                "prompt": "Which Autobus capabilities should this workflow use?",
                "hint": "Select every one the back office should wire into the custom workflow.",
                "required": True,
                "allow_multiple": True,
                "options": [
                    "Business intelligence",
                    "Customer service",
                    "Digital marketing (create content)",
                    "Multi-account posting",
                ],
            },
            {
                "id": "trigger",
                "prompt": "When should the workflow run?",
                "hint": "This becomes the first step. The team can change it later.",
                "placeholder": "Select a trigger",
                "required": True,
                "options": [
                    "When a customer message arrives",
                    "On a schedule",
                    "When someone runs it manually",
                    "When another system calls a webhook",
                ],
            },
            {
                "id": "integrations",
                "prompt": "What should it connect to?",
                "hint": "Optional. We will use what you already have in Autobus unless you say otherwise.",
                "required": False,
                "allow_multiple": True,
                "options": [
                    "Autobus product catalog",
                    "Orders",
                    "Paystack / payments",
                    "Customer list",
                    "Uploaded files / website knowledge",
                    "Email campaigns",
                    "A spreadsheet or another tool",
                ],
            },
            {
                "id": "working_hours",
                "prompt": "When should the agent be available?",
                "placeholder": "Select hours",
                "required": True,
                "options": ["24/7", "Business hours only", "After hours only", "A custom schedule I will share later"],
            },
            {
                "id": "success_looks_like",
                "prompt": "What would success look like in the first 30 days?",
                "hint": "A concrete outcome we can design toward.",
                "placeholder": "e.g. It answers Instagram DMs in under a minute and sends a payment link without a staff member stepping in.",
                "multiline": True,
                "required": True,
            },
            {
                "id": "extra_notes",
                "prompt": "Anything else we should know?",
                "hint": "Optional. Brand rules, things it must never say, or a competitor you want it to beat.",
                "placeholder": "Add any extra context",
                "multiline": True,
                "required": False,
            },
        ]
    }


@agent_routes.post("/custom-request")
def submit_custom_agent_request(
    payload: CustomAgentSubmitRequest,
    db: Session = Depends(get_db),
    authjwt: AuthJWT = Depends(),
):
    """
    Persists custom agent survey responses submitted by portal users into the database
    and creates an AutomationRequest visible in the admin dashboard.
    """
    current_user: Optional[User] = None
    try:
        authjwt.jwt_required()
        current_user = resolve_user_from_jwt(authjwt, db)
    except Exception:
        pass

    answers = payload.answers or {}
    biz_name = (
        answers.get("business_name")
        or (getattr(current_user, "company", None) if current_user else None)
        or "Custom Agent Request"
    )
    goal = answers.get("agent_goal") or answers.get("business_summary") or "Custom agent request"
    industry = answers.get("industry")
    channels = answers.get("channels")

    # Format structured summary for description
    summary_lines = []
    if industry:
        summary_lines.append(f"Industry: {industry}")
    if goal:
        summary_lines.append(f"Primary Goal: {goal}")
    if channels:
        summary_lines.append(f"Channels: {channels}")
    if answers.get("business_summary"):
        summary_lines.append(f"Business Summary: {answers.get('business_summary')}")
    if answers.get("modules"):
        summary_lines.append(f"Modules: {answers.get('modules')}")
    if answers.get("trigger"):
        summary_lines.append(f"Trigger: {answers.get('trigger')}")
    if answers.get("typical_requests"):
        summary_lines.append(f"Typical Requests: {answers.get('typical_requests')}")
    if answers.get("handoff_rules"):
        summary_lines.append(f"Handoff Rules: {answers.get('handoff_rules')}")

    description = (
        "\n".join(summary_lines)
        if summary_lines
        else "Custom agent survey submitted from Autobus Portal."
    )

    contact_name = (
        f"{current_user.first_name} {current_user.last_name}".strip()
        if current_user and getattr(current_user, "first_name", None)
        else (current_user.email if current_user else (biz_name or "Portal User"))
    )

    req = AutomationRequest(
        title=f"Custom workflow: {biz_name}",
        description=description,
        category="Custom Workflow",
        priority="Normal",
        status="Submitted",
        requested_by=contact_name,
        business_name=biz_name,
        contact_email=current_user.email if current_user else None,
        contact_phone=current_user.phone if current_user else None,
        user_id=current_user.id if current_user else None,
        survey_data=answers,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    logger.info("[CUSTOM_AGENT_REQUEST] Created automation request %s for business '%s'", req.id, biz_name)
    return {"id": req.id, "status": req.status}
