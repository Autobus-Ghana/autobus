"""Owner-facing My AI copilot: grounded answers over live business state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.intelligence.service.business_context_assembler import (
    BusinessContext,
    BusinessContextAssembler,
    is_owner_greeting,
    owner_conversation_key,
)
from core.nlu.service.conversation_manager import ConversationManager
from core.nlu.service.llmclient import LLMClient
from core.user.model.User import User
from utilities.plain_text import strip_markdown_formatting

logger = logging.getLogger(__name__)

_OWNER_SYSTEM_PROMPT = """You are the business owner's operations AI for the organization below.
You are speaking with the owner (not a customer). Answer from the live business context only.

Help with: business profile, uploaded files and indexed websites, products and stock,
orders and payments, inbox / customer conversations, and what is currently set up.

Guidelines:
- Be warm, concise, and practical.
- Write plain text only. Never use markdown (no **bold**, headings, or code fences).
- Use "you / your business" when talking to the owner.
- Only state facts that appear in the business context. Do not invent products, prices,
  orders, customers, messages, hours, or policies.
- Quote prices in the business pricing currency from the profile (default GHS). Never use $ unless that is the selected currency.
- If a section is missing or empty, say that data is not in the account yet and how they
  can add it (Intelligence uploads, products, orders, inbox).
- You may mention Autobus features only to help the owner manage this account
  (upload files, index a website, add products, check inbox). Do not describe the
  vendor as if it were their company.
- If the owner greets you, introduce yourself as their business AI and briefly mention
  what you can see in the snapshot (products, open orders, inbox).
- Do not describe Autobus or Greenbrain as the owner's company. You may mention Autobus
  features only to help them manage this account.

Business context:
{context}
"""


@dataclass
class OwnerCopilotResult:
    message: str
    used_llm: bool = False
    sources: List[str] = field(default_factory=list)
    snapshot: Dict[str, Any] = field(default_factory=dict)


class OwnerCopilotService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.assembler = BusinessContextAssembler(db)
        self.conversations = ConversationManager()
        self.llm = LLMClient()

    def chat(self, user: User, message: str) -> OwnerCopilotResult:
        text = (message or "").strip()
        if not text:
            return OwnerCopilotResult(message="Please type a question about your business.")

        conv_key = owner_conversation_key(user.id)
        state = self.conversations.get_conversation_state(conv_key)
        history = list(state.conversation_history or [])
        first_turn = not any(
            (m.get("role") or "") == "user" for m in history if isinstance(m, dict)
        )

        context = self.assembler.assemble(user, text)
        greeting = is_owner_greeting(text) and first_turn

        if greeting:
            reply = _greeting_from_snapshot(user, context)
            self.conversations.update_conversation_history(conv_key, "user", text)
            self.conversations.update_conversation_history(conv_key, "assistant", reply)
            return OwnerCopilotResult(
                message=reply,
                used_llm=False,
                sources=list(context.sources),
                snapshot=dict(context.snapshot),
            )

        credits = CreditService(self.db)
        credits.assert_can_spend(user.id, CreditType.LLM.value, 1.0)

        prompt = _OWNER_SYSTEM_PROMPT.format(context=context.as_prompt_block())
        llm_history = _llm_history(history)
        raw = self.llm.chat_completion(
            system_prompt=prompt,
            user_message=text,
            conversation_history=llm_history,
            temperature=0.2,
            max_tokens=900,
        )
        reply = strip_markdown_formatting(raw).strip()
        if not reply:
            logger.warning("[MY_AI] Empty LLM reply for user %s", user.id)
            return OwnerCopilotResult(
                message="I could not generate a reply just now. Please try again in a moment.",
                used_llm=False,
                sources=list(context.sources),
                snapshot=dict(context.snapshot),
            )

        credits.check_and_deduct(
            user.id,
            CreditType.LLM.value,
            1.0,
            "intelligence_chat",
        )
        self.conversations.update_conversation_history(conv_key, "user", text)
        self.conversations.update_conversation_history(conv_key, "assistant", reply)
        return OwnerCopilotResult(
            message=reply,
            used_llm=True,
            sources=list(context.sources),
            snapshot=dict(context.snapshot),
        )


def _llm_history(history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    cleaned: List[Dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = (item.get("role") or "user").strip()
        if role not in {"user", "assistant"}:
            continue
        content = (item.get("content") or item.get("text") or "").strip()
        if content:
            cleaned.append({"role": role, "content": content})
    return cleaned[-8:]


def _greeting_from_snapshot(user: User, context: BusinessContext) -> str:
    snap = context.snapshot or {}
    owner = (snap.get("owner_name") or user.fullname or "").strip()
    business = (snap.get("business_name") or user.company or "your business").strip()
    first = owner.split()[0] if owner else ""
    hi = f"Hi {first}" if first else "Hi"
    products = int(snap.get("product_count") or 0)
    orders = int(snap.get("active_order_count") or 0)
    waiting = int(snap.get("inbox_waiting_count") or 0)
    has_knowledge = bool(snap.get("has_knowledge"))

    bits = [f"{hi}. I am the AI for {business}."]
    facts = []
    if products:
        facts.append(f"{products} product{'s' if products != 1 else ''} in your catalog")
    else:
        facts.append("no products in your catalog yet")
    if orders:
        facts.append(f"{orders} open order{'s' if orders != 1 else ''}")
    else:
        facts.append("no open orders")
    if waiting:
        facts.append(
            f"{waiting} conversation{'s' if waiting != 1 else ''} waiting in inbox"
        )
    else:
        facts.append("no conversations waiting in inbox")
    if has_knowledge:
        facts.append("indexed files or website pages I can search")
    else:
        facts.append("no uploaded files or indexed website yet")
    bits.append("Right now you have " + "; ".join(facts) + ".")
    bits.append(
        "Ask me about your products, orders, inbox, or anything you have uploaded."
    )
    return " ".join(bits)
