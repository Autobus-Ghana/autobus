"""Owner-facing agentic copilot: tools, confirmations, and media asks."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core.credits.model.credit_types import CreditType
from core.credits.service.credit_service import CreditService
from core.intelligence.dto.agent_turn import (
    AgentActionLog,
    AgentAskChoice,
    AgentAskSpec,
    AgentAttachment,
    AgentConfirmSpec,
    AgentTurnRequest,
    AgentTurnResponse,
)
from core.intelligence.service.business_context_assembler import (
    BusinessContextAssembler,
    is_owner_greeting,
    owner_conversation_key,
)
from core.intelligence.service.owner_agent_campaign import fill_publish_args, load_campaign
from core.intelligence.service.owner_agent_protocol import (
    CONTROL_TOOLS,
    WRITE_TOOLS,
    ask_accept_for,
    compose_user_content,
    confirm_summary,
    confirm_title,
    parse_control_payload,
)
from core.intelligence.service.owner_agent_tools import (
    execute_tool,
    openai_tools,
)
from core.nlu.service.conversation_manager import ConversationManager
from core.nlu.service.llmclient import LLMClient
from core.user.model.User import User
from utilities.plain_text import strip_markdown_formatting

logger = logging.getLogger(__name__)

_MAX_STEPS = 8
_PENDING_KEY = "owner_agent"

_SYSTEM_PROMPT = """You are the AI that runs this owner's business inside Autobus.
You are speaking with the owner, not a customer.

You can look things up and you can take action. For anything that changes data or
sends a message, the app will ask the owner to confirm before it runs.

Help with: products and stock, orders, customers, inbox, messaging, marketing
campaigns (images, videos, captions), posting to linked social accounts
(Instagram, YouTube, TikTok), and knowledge from uploaded files or the
business profile.

Guidelines:
- Be warm, concise, and practical. Plain text only. No markdown.
- Use you / your business.
- Do not invent products, prices, orders, customers, or policies. Use tools.
- Quote prices in the business currency from the snapshot (default GHS).
- If you need a photo, video, document, a choice, or a missing field, call ask_user.
  The owner can attach the same media the dashboard already supports.
- Never claim you already did a write action. Confirmation happens in the app.
- After tools return, tell the owner what you found or what you are ready to do.
- If a section is empty, say so and offer the next step (add a product, upload a file).

Marketing and social posts:
- If they ask for sale content, a poster, flyer, campaign, or something to post,
  call generate_marketing_image unless they clearly want only words or a video/reel.
- generate_marketing_image also returns a caption. Show the image in chat (the app
  renders the media URL). Invite them to post it when they are ready.
- For a video or reel, call generate_marketing_video.
- To post: list_social_accounts first. If none are linked, tell them to connect
  outlets under Marketing, Manage Outlets. If accounts exist, call
  publish_social_post (omit media_urls and caption to reuse the last generated
  campaign). Pass platforms or account_ids when they name a channel; otherwise
  post to every linked account that can take the media. YouTube and TikTok need
  a video. Do not claim it was posted until they confirm in the app.

Business snapshot:
{context}
"""


class OwnerAgentService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.assembler = BusinessContextAssembler(db)
        self.conversations = ConversationManager()
        self.llm = LLMClient()

    def turn(self, user: User, request: AgentTurnRequest) -> AgentTurnResponse:
        conv_key = owner_conversation_key(user.id)
        state = self.conversations.get_conversation_state(conv_key)
        pending = _pending_from_state(state.pending_action)

        if request.confirm_id:
            return self._resume_confirm(user, conv_key, request)

        if request.ask_id or (
            pending
            and pending.get("kind") == "ask"
            and ((request.message or "").strip() or request.attachments)
        ):
            return self._resume_ask(user, conv_key, request)

        if pending and pending.get("kind") == "confirm":
            self._set_pending(conv_key, None)

        text = (request.message or "").strip()
        history = list(state.conversation_history or [])
        first_turn = not any(
            (m.get("role") or "") == "user" for m in history if isinstance(m, dict)
        )
        if (
            first_turn
            and not request.attachments
            and (not text or is_owner_greeting(text))
        ):
            context = self.assembler.assemble(user, text or "hello")
            reply = _agent_greeting(user, context.snapshot)
            self._remember(conv_key, "user", text or "hello")
            self._remember(conv_key, "assistant", reply)
            self._set_pending(conv_key, None)
            return AgentTurnResponse(
                message=reply,
                used_llm=False,
                turn_type="reply",
                sources=list(context.sources),
            )

        credits = CreditService(self.db)
        credits.assert_can_spend(user.id, CreditType.LLM.value, 1.0)

        user_content = compose_user_content(text, request.attachments)
        self._remember(conv_key, "user", user_content)
        result = self._run_loop(user, user_content, image_url=_first_image_url(request.attachments))
        credits.check_and_deduct(user.id, CreditType.LLM.value, 1.0, "owner_agent")
        return result

    def _resume_confirm(self, user: User, conv_key: str, request: AgentTurnRequest) -> AgentTurnResponse:
        state = self.conversations.get_conversation_state(conv_key)
        pending = _pending_from_state(state.pending_action)
        if not pending or pending.get("kind") != "confirm" or pending.get("id") != request.confirm_id:
            return AgentTurnResponse(
                message="There is nothing waiting for confirmation. Tell me what you would like to do.",
                turn_type="reply",
            )

        title = pending.get("title") or confirm_title(
            str(pending.get("tool") or ""),
            pending.get("args") if isinstance(pending.get("args"), dict) else {},
        )
        if request.confirmed is False:
            self._remember(conv_key, "user", f"[Owner declined: {title}]")
            self._set_pending(conv_key, None)
            reply = f"Okay, cancelled. I did not go ahead with {title.rstrip('?')}."
            self._remember(conv_key, "assistant", reply)
            return AgentTurnResponse(message=reply, turn_type="reply")

        tool = str(pending.get("tool") or "")
        args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
        raw = execute_tool(self.db, user, tool, args)
        parsed = _as_dict(raw)
        ok = bool(parsed.get("ok", True)) if parsed else True
        self._remember(
            conv_key,
            "user",
            f"[Owner approved: {title}]",
        )
        self._set_pending(conv_key, None)
        follow = (
            f"The owner approved {tool}. Result:\n{raw}\n"
            "Reply in one or two short sentences. If it failed, say what they should do next. "
            "Do not claim success unless ok is true."
        )
        credits = CreditService(self.db)
        credits.assert_can_spend(user.id, CreditType.LLM.value, 1.0)
        result = self._run_loop(user, follow)
        credits.check_and_deduct(user.id, CreditType.LLM.value, 1.0, "owner_agent")
        if result.actions:
            result.actions.insert(0, AgentActionLog(tool=tool, ok=ok, detail=_action_detail(parsed, raw)))
        else:
            result.actions = [AgentActionLog(tool=tool, ok=ok, detail=_action_detail(parsed, raw))]
        return result

    def _resume_ask(self, user: User, conv_key: str, request: AgentTurnRequest) -> AgentTurnResponse:
        state = self.conversations.get_conversation_state(conv_key)
        pending = _pending_from_state(state.pending_action)
        if request.ask_id and (
            not pending or pending.get("kind") != "ask" or pending.get("id") != request.ask_id
        ):
            return AgentTurnResponse(
                message="I was not waiting on that prompt. Tell me what you would like to do.",
                turn_type="reply",
            )

        user_content = compose_user_content(request.message, request.attachments)
        prompt = (pending or {}).get("prompt") or "your request"
        wrapped = f"[Owner answered: {prompt}]\n{user_content}"
        self._remember(conv_key, "user", wrapped)
        self._set_pending(conv_key, None)

        credits = CreditService(self.db)
        credits.assert_can_spend(user.id, CreditType.LLM.value, 1.0)
        result = self._run_loop(user, wrapped, image_url=_first_image_url(request.attachments))
        credits.check_and_deduct(user.id, CreditType.LLM.value, 1.0, "owner_agent")
        return result

    def _run_loop(
        self,
        user: User,
        user_content: str,
        image_url: Optional[str] = None,
    ) -> AgentTurnResponse:
        conv_key = owner_conversation_key(user.id)
        context = self.assembler.assemble(user, user_content)
        system = _SYSTEM_PROMPT.format(context=context.as_prompt_block())
        campaign = load_campaign(user)
        campaign_bits = {
            k: campaign.get(k)
            for k in ("kind", "url", "media_urls", "caption")
            if campaign.get(k)
        }
        if campaign_bits:
            system += (
                "\n\nLast generated campaign (reuse for Instagram unless they ask for new content):\n"
                + json.dumps(campaign_bits, default=str)
            )
        state = self.conversations.get_conversation_state(conv_key)
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(_llm_history(state.conversation_history or []))
        messages.append(_user_message(user_content, image_url))

        actions: List[AgentActionLog] = []
        attachments: List[AgentAttachment] = []
        tools = openai_tools()

        for _ in range(_MAX_STEPS):
            assistant = self.llm.chat_with_tools(
                messages,
                tools=tools,
                temperature=0.2,
                max_tokens=900,
            )
            content = strip_markdown_formatting(getattr(assistant, "content", None) or "").strip()
            tool_calls = list(getattr(assistant, "tool_calls", None) or [])

            if not tool_calls:
                control = parse_control_payload(content)
                if control:
                    handled = self._handle_control_json(
                        user, conv_key, control, content, actions, context.sources, attachments
                    )
                    if handled is not None:
                        return handled
                reply = content or "I could not generate a reply just now. Please try again."
                self._remember(conv_key, "assistant", reply)
                self._set_pending(conv_key, None)
                return AgentTurnResponse(
                    message=reply,
                    used_llm=True,
                    turn_type="reply",
                    actions=actions,
                    sources=list(context.sources),
                    attachments=attachments,
                )

            read_calls = []
            control_call = None
            for call in tool_calls:
                name, args, _ = _split_tool_call(call)
                if name in CONTROL_TOOLS or name in WRITE_TOOLS:
                    if control_call is None:
                        control_call = call
                else:
                    read_calls.append(call)

            if read_calls and control_call is None:
                messages.append(_assistant_tool_message(assistant, content, tool_calls))
                for call in read_calls:
                    name, args, call_id = _split_tool_call(call)
                    raw = execute_tool(self.db, user, name, args)
                    parsed = _as_dict(raw)
                    ok = bool(parsed.get("ok", True)) if parsed else True
                    actions.append(AgentActionLog(tool=name, ok=ok, detail=_action_detail(parsed, raw)))
                    attachments.extend(_attachments_from_tool(name, parsed))
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": raw})
                continue

            if read_calls:
                for call in read_calls:
                    name, args, _ = _split_tool_call(call)
                    raw = execute_tool(self.db, user, name, args)
                    parsed = _as_dict(raw)
                    ok = bool(parsed.get("ok", True)) if parsed else True
                    actions.append(AgentActionLog(tool=name, ok=ok, detail=_action_detail(parsed, raw)))
                    attachments.extend(_attachments_from_tool(name, parsed))

            name, args, _ = _split_tool_call(control_call)
            spoken = content or _spoken_for_control(name, args)
            if name == "ask_user":
                return self._pause_ask(conv_key, spoken, args, actions, context.sources)
            if name in {"publish_instagram_post", "publish_social_post"}:
                args = fill_publish_args(
                    user,
                    args,
                    db=self.db,
                    instagram_only=name == "publish_instagram_post",
                )
            return self._pause_confirm(
                conv_key, spoken, name, args, actions, context.sources, attachments
            )

        fallback = content if "content" in locals() and content else (
            "I need another moment. Please send that again."
        )
        self._remember(conv_key, "assistant", fallback)
        return AgentTurnResponse(
            message=fallback,
            used_llm=True,
            turn_type="reply",
            actions=actions,
            sources=list(context.sources),
            attachments=attachments,
        )

    def _handle_control_json(
        self,
        user: User,
        conv_key: str,
        control: Dict[str, Any],
        content: str,
        actions: List[AgentActionLog],
        sources: List[str],
        attachments: Optional[List[AgentAttachment]] = None,
    ) -> Optional[AgentTurnResponse]:
        kind = (control.get("type") or "").strip().lower()
        message = strip_markdown_formatting(str(control.get("message") or content)).strip()
        media = list(attachments or [])
        if kind == "ask_input" or kind == "ask_user":
            ask = control.get("ask") if isinstance(control.get("ask"), dict) else control
            return self._pause_ask(conv_key, message, ask, actions, sources)
        if kind == "confirm":
            tool = str(control.get("tool") or "").strip()
            args = control.get("args") if isinstance(control.get("args"), dict) else {}
            if tool in WRITE_TOOLS:
                if tool in {"publish_instagram_post", "publish_social_post"}:
                    args = fill_publish_args(
                        user,
                        args,
                        db=self.db,
                        instagram_only=tool == "publish_instagram_post",
                    )
                return self._pause_confirm(conv_key, message, tool, args, actions, sources, media)
        if kind == "call_tool":
            tool = str(control.get("tool") or "").strip()
            args = control.get("args") if isinstance(control.get("args"), dict) else {}
            if tool == "ask_user":
                return self._pause_ask(conv_key, message, args, actions, sources)
            if tool in WRITE_TOOLS:
                if tool in {"publish_instagram_post", "publish_social_post"}:
                    args = fill_publish_args(
                        user,
                        args,
                        db=self.db,
                        instagram_only=tool == "publish_instagram_post",
                    )
                return self._pause_confirm(conv_key, message, tool, args, actions, sources, media)
            raw = execute_tool(self.db, user, tool, args)
            parsed = _as_dict(raw)
            ok = bool(parsed.get("ok", True)) if parsed else True
            actions.append(AgentActionLog(tool=tool, ok=ok, detail=_action_detail(parsed, raw)))
            media.extend(_attachments_from_tool(tool, parsed))
            follow = f"Tool {tool} returned:\n{raw}\nReply briefly to the owner."
            self._remember(conv_key, "user", follow)
            return self._run_loop(user, follow)
        if kind == "reply" and message:
            self._remember(conv_key, "assistant", message)
            self._set_pending(conv_key, None)
            return AgentTurnResponse(
                message=message,
                used_llm=True,
                turn_type="reply",
                actions=actions,
                sources=list(sources),
                attachments=media,
            )
        return None

    def _pause_ask(
        self,
        conv_key: str,
        spoken: str,
        args: Dict[str, Any],
        actions: List[AgentActionLog],
        sources: List[str],
    ) -> AgentTurnResponse:
        kind = str(args.get("kind") or "text").strip().lower()
        if kind not in {"text", "image", "video", "file", "choice"}:
            kind = "text"
        prompt = str(args.get("prompt") or "Please provide this.").strip()
        ask_id = str(uuid.uuid4())
        choices = []
        for item in args.get("choices") or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            label = str(item.get("label") or cid).strip()
            if cid and label:
                choices.append(AgentAskChoice(id=cid, label=label))
        message = spoken or prompt
        self._remember(conv_key, "assistant", message)
        self._set_pending(
            conv_key,
            {
                "kind": "ask",
                "id": ask_id,
                "prompt": prompt,
                "ask_kind": kind,
            },
        )
        return AgentTurnResponse(
            message=message,
            used_llm=True,
            turn_type="ask_input",
            ask=AgentAskSpec(
                id=ask_id,
                kind=kind,
                prompt=prompt,
                accept=ask_accept_for(kind),
                choices=choices,
            ),
            actions=actions,
            sources=list(sources),
        )

    def _pause_confirm(
        self,
        conv_key: str,
        spoken: str,
        tool: str,
        args: Dict[str, Any],
        actions: List[AgentActionLog],
        sources: List[str],
        extra_attachments: Optional[List[AgentAttachment]] = None,
    ) -> AgentTurnResponse:
        confirm_id = str(uuid.uuid4())
        title = confirm_title(tool, args)
        summary = confirm_summary(tool, args)
        message = spoken or f"{title} {summary}"
        media = list(extra_attachments or [])
        media.extend(_attachments_from_publish_args(args))
        self._remember(conv_key, "assistant", message)
        self._set_pending(
            conv_key,
            {
                "kind": "confirm",
                "id": confirm_id,
                "tool": tool,
                "args": args,
                "title": title,
            },
        )
        return AgentTurnResponse(
            message=message,
            used_llm=True,
            turn_type="confirm",
            confirm=AgentConfirmSpec(
                id=confirm_id,
                title=title,
                summary=summary,
                tool=tool,
                payload=args,
            ),
            actions=actions,
            sources=list(sources),
            attachments=media,
        )

    def _remember(self, conv_key: str, role: str, content: str) -> None:
        text = (content or "").strip()
        if not text:
            return
        self.conversations.update_conversation_history(conv_key, role, text)

    def _set_pending(self, conv_key: str, payload: Optional[Dict[str, Any]]) -> None:
        state = self.conversations.get_conversation_state(conv_key)
        current = dict(state.pending_action or {}) if isinstance(state.pending_action, dict) else {}
        if payload is None:
            current.pop(_PENDING_KEY, None)
            state.pending_action = current or None
        else:
            current[_PENDING_KEY] = payload
            state.pending_action = current
        self.conversations.persist(conv_key)


def _pending_from_state(pending_action: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(pending_action, dict):
        return None
    nested = pending_action.get(_PENDING_KEY)
    if isinstance(nested, dict):
        return nested
    if pending_action.get("kind") in {"ask", "confirm"}:
        return pending_action
    return None


def _first_image_url(attachments: Optional[List[AgentAttachment]]) -> Optional[str]:
    for item in attachments or []:
        kind = (item.kind or "").lower()
        url = (item.url or "").strip()
        if url and kind in {"image", "photo", "picture"}:
            return url
    return None


def _attachments_from_tool(name: str, parsed: Optional[Dict[str, Any]]) -> List[AgentAttachment]:
    if not parsed or parsed.get("ok") is False:
        return []
    data = parsed.get("data")
    if not isinstance(data, dict):
        return []
    url = str(data.get("url") or "").strip()
    kind = str(data.get("kind") or "").strip().lower()
    if name == "generate_marketing_video":
        kind = kind or "video"
    elif name == "generate_marketing_image":
        kind = kind or "image"
    if not url or kind not in {"image", "video"}:
        return []
    return [
        AgentAttachment(
            kind=kind,
            url=url,
            name=str(data.get("name") or "").strip() or None,
            mime=str(data.get("mime") or "").strip() or None,
        )
    ]


def _attachments_from_publish_args(args: Dict[str, Any]) -> List[AgentAttachment]:
    urls = args.get("media_urls") if isinstance(args, dict) else None
    if not isinstance(urls, list):
        return []
    out: List[AgentAttachment] = []
    for item in urls:
        url = str(item or "").strip()
        if not url:
            continue
        lower = url.lower()
        kind = "video" if any(lower.endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm")) else "image"
        out.append(AgentAttachment(kind=kind, url=url))
    return out


def _user_message(text: str, image_url: Optional[str]) -> Dict[str, Any]:
    if not image_url:
        return {"role": "user", "content": text}
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }


def _assistant_tool_message(assistant: Any, content: str, tool_calls: List[Any]) -> Dict[str, Any]:
    serialized = []
    for call in tool_calls:
        name, args, call_id = _split_tool_call(call)
        serialized.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        )
    return {"role": "assistant", "content": content or None, "tool_calls": serialized}


def _split_tool_call(call: Any) -> Tuple[str, Dict[str, Any], str]:
    if isinstance(call, dict):
        fn = call.get("function") or {}
        name = str(fn.get("name") or call.get("name") or "")
        raw_args = fn.get("arguments") or call.get("arguments") or "{}"
        call_id = str(call.get("id") or uuid.uuid4())
    else:
        fn = getattr(call, "function", None)
        name = str(getattr(fn, "name", "") or getattr(call, "name", "") or "")
        raw_args = getattr(fn, "arguments", None) or "{}"
        call_id = str(getattr(call, "id", None) or uuid.uuid4())
    args: Dict[str, Any] = {}
    if isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            parsed = json.loads(raw_args or "{}")
            if isinstance(parsed, dict):
                args = parsed
        except json.JSONDecodeError:
            args = {}
    return name, args, call_id


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
    return cleaned[-10:]


def _as_dict(raw: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _action_detail(parsed: Optional[Dict[str, Any]], raw: str) -> str:
    if not parsed:
        return (raw or "")[:240]
    if parsed.get("ok") is False:
        return str(parsed.get("error") or raw)[:240]
    data = parsed.get("data")
    if isinstance(data, dict) and data.get("message"):
        return str(data.get("message"))[:240]
    return "ok"


def _spoken_for_control(name: str, args: Dict[str, Any]) -> str:
    if name == "ask_user":
        return str(args.get("prompt") or "I need a bit more from you.")
    return confirm_title(name, args)


def _agent_greeting(user: User, snapshot: Dict[str, Any]) -> str:
    owner = (snapshot.get("owner_name") or user.fullname or "").strip()
    business = (snapshot.get("business_name") or user.company or "your business").strip()
    first = owner.split()[0] if owner else ""
    hi = f"Hi {first}" if first else "Hi"
    products = int(snapshot.get("product_count") or 0)
    orders = int(snapshot.get("active_order_count") or 0)
    waiting = int(snapshot.get("inbox_waiting_count") or 0)
    bits = [
        f"{hi}. I am the AI running {business}.",
        "Speak or type what you want done — products, orders, customers, inbox, or a campaign.",
    ]
    facts = []
    if products:
        facts.append(f"{products} product{'s' if products != 1 else ''} in the catalog")
    else:
        facts.append("no products in the catalog yet")
    if orders:
        facts.append(f"{orders} open order{'s' if orders != 1 else ''}")
    if waiting:
        facts.append(f"{waiting} conversation{'s' if waiting != 1 else ''} waiting in inbox")
    bits.append("Right now: " + "; ".join(facts) + ".")
    bits.append("I will ask when I need a photo, a video, or your go-ahead.")
    return " ".join(bits)
