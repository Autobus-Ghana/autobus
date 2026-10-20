"""Provider settings and workflow generation for the admin AI assistant."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core.ai_workflow.dto.ai_workflow_dto import (
    AiSettingsResponse,
    AiSettingsTestResponse,
    AiSettingsUpdateRequest,
    GenerateWorkflowRequest,
    GenerateWorkflowResponse,
)
from core.ai_workflow.model.ai_workflow import AIWorkflowAssistantSettings
from utilities.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

SETTINGS_ID = "default"
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-6.1-sol"

AI_MODELS: Dict[str, set[str]] = {
    "openai": {"gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"},
    "anthropic": {"claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"},
    "google": {"gemini-3.8-flash", "gemini-4-argon", "gemini-3.5-flash-lite"},
}


class AIAssistantService:
    def __init__(self, db: Session):
        self.db = db

    def get_settings(self) -> AiSettingsResponse:
        row = self._row()
        if not row:
            return AiSettingsResponse(
                provider=DEFAULT_PROVIDER,
                model=DEFAULT_MODEL,
                apiKeyConfigured=False,
            )
        return AiSettingsResponse(
            provider=row.provider,
            model=row.model,
            apiKeyConfigured=bool(row.api_key_encrypted),
        )

    def save_settings(self, payload: AiSettingsUpdateRequest) -> AiSettingsResponse:
        provider = (payload.provider or "").strip()
        model = (payload.model or "").strip()
        self._require_known_model(provider, model)
        incoming_key = (payload.apiKey or "").strip()
        row = self._row()

        if row and row.provider != provider and not incoming_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Enter an API key for the selected provider.",
            )
        if not row and not incoming_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Enter an API key for the selected provider.",
            )
        if not row:
            row = AIWorkflowAssistantSettings(
                id=SETTINGS_ID,
                provider=provider,
                model=model,
            )
            self.db.add(row)

        row.provider = provider
        row.model = model
        if incoming_key:
            try:
                row.api_key_encrypted = encrypt_secret(incoming_key)
            except RuntimeError as exc:
                logger.error("AI assistant key encryption failed")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="The server cannot store API keys until token encryption is configured.",
                ) from exc

        self.db.commit()
        self.db.refresh(row)
        return AiSettingsResponse(
            provider=row.provider,
            model=row.model,
            apiKeyConfigured=bool(row.api_key_encrypted),
        )

    def test_connection(self) -> AiSettingsTestResponse:
        provider, model, api_key = self._credentials()
        try:
            message = self._ping_provider(provider, model, api_key)
        except HTTPException:
            raise
        except Exception:
            logger.exception("AI assistant connection test failed")
            return AiSettingsTestResponse(
                success=False,
                message="The provider could not be reached. Check the API key and model, then try again.",
            )
        return AiSettingsTestResponse(success=True, message=message)

    def generate_workflow(self, payload: GenerateWorkflowRequest) -> GenerateWorkflowResponse:
        provider, model, api_key = self._credentials()
        supported = [item.strip() for item in payload.supportedNodeTypes if item and item.strip()]
        if not supported:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="supportedNodeTypes is required.",
            )
        raw = self._complete(
            provider,
            model,
            api_key,
            self._generation_prompt(payload.businessName.strip(), payload.prompt.strip(), supported),
        )
        try:
            parsed = _extract_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The AI returned a response that was not valid workflow JSON.",
            ) from exc
        workflow = parsed.get("workflow") if isinstance(parsed, dict) else None
        description = parsed.get("description") if isinstance(parsed, dict) else None
        validated = _validate_workflow(workflow, supported)
        if description is not None and not isinstance(description, str):
            description = None
        return GenerateWorkflowResponse(workflow=validated, description=description)

    def _row(self) -> Optional[AIWorkflowAssistantSettings]:
        return (
            self.db.query(AIWorkflowAssistantSettings)
            .filter(AIWorkflowAssistantSettings.id == SETTINGS_ID)
            .first()
        )

    def _credentials(self) -> tuple[str, str, str]:
        row = self._row()
        if not row or not row.api_key_encrypted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Save an API key in AI Settings before continuing.",
            )
        api_key = (decrypt_secret(row.api_key_encrypted) or "").strip()
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The saved API key could not be read. Enter it again in AI Settings.",
            )
        return row.provider, row.model, api_key

    def _require_known_model(self, provider: str, model: str) -> None:
        allowed = AI_MODELS.get(provider)
        if not allowed or model not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Choose a supported provider and model.",
            )

    def _ping_provider(self, provider: str, model: str, api_key: str) -> str:
        if provider == "openai":
            _post_json(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with ok"}],
                    "max_completion_tokens": 16,
                },
            )
            return f"Connected to OpenAI with {model}."
        if provider == "anthropic":
            _post_json(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                payload={
                    "model": model,
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "Reply with ok"}],
                },
            )
            return f"Connected to Anthropic with {model}."
        if provider == "google":
            _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={},
                payload={"contents": [{"parts": [{"text": "Reply with ok"}]}]},
                params={"key": api_key},
            )
            return f"Connected to Google Gemini with {model}."
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a supported provider and model.",
        )

    def _complete(self, provider: str, model: str, api_key: str, prompt: str) -> str:
        if provider == "openai":
            body = _post_json(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "Return only JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                },
                timeout=90,
            )
            return str(body["choices"][0]["message"]["content"])
        if provider == "anthropic":
            body = _post_json(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                payload={
                    "model": model,
                    "max_tokens": 8000,
                    "system": "Return only JSON.",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )
            parts = body.get("content") or []
            texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
            return "\n".join(texts)
        if provider == "google":
            body = _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={},
                payload={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
                params={"key": api_key},
                timeout=90,
            )
            candidates = body.get("candidates") or []
            parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
            texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
            return "\n".join(texts)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a supported provider and model.",
        )

    def _generation_prompt(self, business_name: str, prompt: str, supported: List[str]) -> str:
        types = "\n".join(f"- {item}" for item in supported)
        return (
            "Design one n8n workflow for this business. Return a JSON object with "
            '"workflow" and "description".\n'
            "workflow.name is a short title.\n"
            "workflow.nodes is a non-empty array. Each node needs id, name, type, "
            "typeVersion, position as [x, y], and parameters as an object.\n"
            "workflow.connections maps a source node name to connection groups. "
            'Use {"main": [[{"node": "Target Name", "type": "main", "index": 0}]]}.\n'
            "Every connection target must be the name of a node in workflow.nodes.\n"
            "Start with a trigger node and use only these node types:\n"
            f"{types}\n"
            f"Business: {business_name}\n"
            f"Request: {prompt}"
        )


def _post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    params: Optional[Dict[str, str]] = None,
    timeout: float = 30,
) -> Dict[str, Any]:
    try:
        response = httpx.post(url, headers=headers, json=payload, params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI provider could not be reached.",
        ) from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_provider_error(response),
        )
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI provider returned an unreadable response.",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI provider returned an unreadable response.",
        )
    return body


def _provider_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = None
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(error, str):
            message = error
        elif isinstance(body.get("message"), str):
            message = body["message"]
    message = re.sub(r"sk-[A-Za-z0-9_\-]+", "[redacted]", message or "")
    message = message.strip()
    if not message:
        return f"The AI provider rejected the request (HTTP {response.status_code})."
    return message[:500]


def _extract_json(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Workflow JSON must be an object.")
    return parsed


def _validate_workflow(workflow: Any, supported: List[str]) -> Dict[str, Any]:
    if not isinstance(workflow, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI returned an invalid workflow.",
        )
    name = workflow.get("name")
    nodes = workflow.get("nodes")
    connections = workflow.get("connections")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI returned a workflow without a name.",
        )
    if not isinstance(nodes, list) or not nodes:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI returned a workflow without nodes.",
        )
    if not isinstance(connections, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI returned a workflow without connections.",
        )

    allowed = set(supported)
    names = set()
    cleaned_nodes: List[Dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The AI returned a workflow node that is not an object.",
            )
        node_name = node.get("name")
        node_type = node.get("type")
        position = node.get("position")
        parameters = node.get("parameters")
        type_version = node.get("typeVersion", 1)
        if (
            not isinstance(node_name, str)
            or not node_name.strip()
            or not isinstance(node_type, str)
            or node_type not in allowed
            or not isinstance(position, list)
            or len(position) != 2
            or not all(isinstance(item, (int, float)) for item in position)
            or not isinstance(parameters, dict)
            or isinstance(type_version, bool)
            or not isinstance(type_version, (int, float))
        ):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The AI generated a node this editor cannot safely edit.",
            )
        names.add(node_name)
        cleaned_nodes.append(
            {
                "id": node.get("id") if isinstance(node.get("id"), str) and node.get("id") else f"node-{index + 1}",
                "name": node_name,
                "type": node_type,
                "typeVersion": type_version,
                "position": [position[0], position[1]],
                "parameters": parameters,
            }
        )

    for source, groups in connections.items():
        if not isinstance(groups, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The AI generated invalid workflow connections.",
            )
        for branches in groups.values():
            if not isinstance(branches, list) or any(
                not isinstance(branch, list)
                or any(
                    not isinstance(target, dict)
                    or not isinstance(target.get("node"), str)
                    or target.get("node") not in names
                    for target in branch
                )
                for branch in branches
            ):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="The AI generated invalid workflow connections.",
                )

    return {
        "name": name.strip(),
        "nodes": cleaned_nodes,
        "connections": connections,
        "active": False,
        "settings": {"executionOrder": "v1"},
    }
