"""Workflow generation for the admin AI assistant.

The assistant uses the platform language model already configured for Autobus
(`LLM_MODEL` on OpenRouter, or `LLM_BASE_URL`). It does not accept a separate
provider key or model catalog.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core.ai_workflow.dto.ai_workflow_dto import (
    AiSettingsResponse,
    AiSettingsTestResponse,
    AiSettingsUpdateRequest,
    GenerateWorkflowRequest,
    GenerateWorkflowResponse,
)

logger = logging.getLogger(__name__)


class AIAssistantService:
    def __init__(self, db: Session):
        self.db = db

    def get_settings(self) -> AiSettingsResponse:
        provider, model, configured = _platform()
        return AiSettingsResponse(
            provider=provider,
            model=model,
            apiKeyConfigured=configured,
        )

    def save_settings(self, payload: AiSettingsUpdateRequest) -> AiSettingsResponse:
        _ = payload
        return self.get_settings()

    def test_connection(self) -> AiSettingsTestResponse:
        _provider, model, configured = _platform()
        if not configured:
            return AiSettingsTestResponse(
                success=False,
                message="The platform language model is not configured.",
            )
        try:
            self._complete("Reply with ok", "Reply with ok.", max_tokens=16)
        except HTTPException as exc:
            return AiSettingsTestResponse(success=False, message=str(exc.detail))
        except Exception:
            logger.exception("AI assistant connection test failed")
            return AiSettingsTestResponse(
                success=False,
                message="The platform language model could not be reached.",
            )
        return AiSettingsTestResponse(success=True, message=f"Connected with {model}.")

    def generate_workflow(self, payload: GenerateWorkflowRequest) -> GenerateWorkflowResponse:
        _provider, model, configured = _platform()
        if not configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The platform language model is not configured.",
            )
        supported = [item.strip() for item in payload.supportedNodeTypes if item and item.strip()]
        if not supported:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="supportedNodeTypes is required.",
            )
        raw = self._complete(
            self._generation_prompt(payload.businessName.strip(), payload.prompt.strip(), supported, model),
            "Return only JSON.",
            max_tokens=8000,
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
        _stamp_platform_model(validated, model)
        if description is not None and not isinstance(description, str):
            description = None
        return GenerateWorkflowResponse(workflow=validated, description=description)

    def _complete(self, prompt: str, system: str, max_tokens: int) -> str:
        try:
            from core.nlu.config import MODEL, openai_client
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The platform language model is not configured.",
            ) from exc
        try:
            response = openai_client().chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_completion_tokens=max_tokens,
            )
        except Exception as exc:
            logger.exception("Platform language model request failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=_safe_error(exc),
            ) from exc
        message = response.choices[0].message if response.choices else None
        content = getattr(message, "content", "") if message else ""
        text = content.strip() if isinstance(content, str) else str(content or "").strip()
        if not text:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The platform language model returned an empty response.",
            )
        return text

    def _generation_prompt(self, business_name: str, prompt: str, supported: List[str], model: str) -> str:
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
            f"For every chat-model or agent node, set parameters.model and parameters.modelName to {model}.\n"
            f"Business: {business_name}\n"
            f"Request: {prompt}"
        )


def _platform() -> tuple[str, str, bool]:
    try:
        from core.nlu.config import LLM_API_KEY, LLM_BASE_URL, MODEL
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The platform language model is not configured.",
        ) from exc
    model = (MODEL or "").strip()
    if not model:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The platform language model is not configured.",
        )
    base = (LLM_BASE_URL or "").lower()
    if "openrouter.ai" in base:
        provider = "openrouter"
    elif "groq.com" in base:
        provider = "groq"
    else:
        provider = "openai"
    return provider, model, bool((LLM_API_KEY or "").strip())


def _stamp_platform_model(workflow: Dict[str, Any], model: str) -> None:
    for node in workflow.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "")
        parameters = node.get("parameters")
        if not isinstance(parameters, dict) or "langchain" not in node_type:
            continue
        parameters["model"] = model
        parameters["modelName"] = model


def _safe_error(exc: Exception) -> str:
    message = re.sub(r"sk-[A-Za-z0-9_\-]+", "[redacted]", str(exc) or "")
    message = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", message, flags=re.IGNORECASE)
    message = message.strip()
    if not message:
        return "The platform language model rejected the request."
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
