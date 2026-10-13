import asyncio
from typing import Any, Dict, Optional

import httpx

from config import settings


class N8nAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class N8nClient:
    """Thin wrapper over the n8n public REST API (/api/v1) plus webhook triggering."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_s: float = 30.0,
    ):
        self.base_url = (base_url or settings.N8N_BASE_URL).rstrip("/")
        self.api_key = (api_key if api_key is not None else settings.N8N_API_KEY).strip()
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Any:
        if auth and not self.configured:
            raise N8nAPIError(503, "n8n is not configured on the server (N8N_API_KEY missing).")
        headers = {"Accept": "application/json"}
        if auth:
            headers["X-N8N-API-KEY"] = self.api_key
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                res = await client.request(
                    method, f"{self.base_url}{path}", headers=headers, json=json, params=params
                )
        except httpx.HTTPError as exc:
            raise N8nAPIError(502, f"Could not reach n8n: {exc.__class__.__name__}") from exc

        body: Any
        try:
            body = res.json() if res.content else {}
        except ValueError:
            body = {"raw": res.text[:2000]}
        if res.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else None
            raise N8nAPIError(res.status_code, message or f"n8n returned HTTP {res.status_code}")
        return body

    async def healthy(self) -> bool:
        try:
            await self._request("GET", "/healthz", auth=False)
            return True
        except N8nAPIError:
            return False

    async def list_workflows(self, limit: int = 100, cursor: Optional[str] = None) -> Any:
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/api/v1/workflows", params=params)

    async def get_workflow(self, workflow_id: str) -> Any:
        return await self._request("GET", f"/api/v1/workflows/{workflow_id}")

    async def create_workflow(self, payload: Dict[str, Any]) -> Any:
        return await self._request("POST", "/api/v1/workflows", json=payload)

    async def update_workflow(self, workflow_id: str, payload: Dict[str, Any]) -> Any:
        return await self._request("PUT", f"/api/v1/workflows/{workflow_id}", json=payload)

    async def delete_workflow(self, workflow_id: str) -> Any:
        # n8n 2.x refuses to delete a published (active) workflow.
        workflow = await self.get_workflow(workflow_id)
        if workflow.get("active"):
            await self.set_active(workflow_id, False)
        # Unpublishing completes asynchronously; n8n asks callers to retry shortly.
        for attempt in range(10):
            try:
                return await self._request("DELETE", f"/api/v1/workflows/{workflow_id}")
            except N8nAPIError as exc:
                if attempt == 9 or "try again" not in str(exc).lower():
                    raise
                await asyncio.sleep(1)

    async def set_active(self, workflow_id: str, active: bool) -> Any:
        action = "activate" if active else "deactivate"
        return await self._request("POST", f"/api/v1/workflows/{workflow_id}/{action}")

    async def list_executions(
        self, workflow_id: Optional[str] = None, limit: int = 20, status: Optional[str] = None
    ) -> Any:
        params: Dict[str, Any] = {"limit": limit}
        if workflow_id:
            params["workflowId"] = workflow_id
        if status:
            params["status"] = status
        return await self._request("GET", "/api/v1/executions", params=params)

    async def get_execution(self, execution_id: str, include_data: bool = False) -> Any:
        return await self._request(
            "GET",
            f"/api/v1/executions/{execution_id}",
            params={"includeData": str(include_data).lower()},
        )

    async def trigger_webhook(self, path: str, payload: Any, test: bool = False) -> Any:
        prefix = "webhook-test" if test else "webhook"
        return await self._request(
            "POST", f"/{prefix}/{path.lstrip('/')}", json=payload, auth=False
        )
