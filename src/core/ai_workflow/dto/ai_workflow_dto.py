from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class BusinessCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Business folder name")
    description: Optional[str] = Field(None, max_length=1000, description="Optional description")


class BusinessResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    workflow_count: int = 0
    created_at: str
    updated_at: Optional[str] = None


class WorkflowStatsDto(BaseModel):
    totalRuns: int = 0
    lastRunAt: Optional[str] = "Never"
    lastRunStatus: Optional[str] = "idle"


class WorkflowCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    business_name: Optional[str] = Field(default=None, alias="businessName")
    businessName: Optional[str] = None
    description: Optional[str] = ""
    category: str = "General"
    active: bool = True
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)
    n8n_id: Optional[str] = Field(default=None, alias="n8nId")
    n8nId: Optional[str] = None
    stats: Optional[Dict[str, Any]] = None

    def resolved_business_name(self) -> str:
        return (self.business_name or self.businessName or "General").strip()

    def resolved_n8n_id(self) -> Optional[str]:
        return self.n8n_id or self.n8nId


class WorkflowUpdateRequest(BaseModel):
    name: Optional[str] = None
    business_name: Optional[str] = Field(default=None, alias="businessName")
    businessName: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    active: Optional[bool] = None
    nodes: Optional[List[Dict[str, Any]]] = None
    edges: Optional[List[Dict[str, Any]]] = None
    n8n_id: Optional[str] = Field(default=None, alias="n8nId")
    n8nId: Optional[str] = None
    stats: Optional[Dict[str, Any]] = None

    def resolved_business_name(self) -> Optional[str]:
        val = self.business_name or self.businessName
        return val.strip() if val else None

    def resolved_n8n_id(self) -> Optional[str]:
        return self.n8n_id or self.n8nId


class WorkflowSyncN8nRequest(BaseModel):
    n8n_id: Optional[str] = Field(default=None, alias="n8nId")
    n8nId: Optional[str] = None

    def resolved_id(self) -> str:
        val = self.n8n_id or self.n8nId
        return (val or "").strip()


class WorkflowToggleActiveRequest(BaseModel):
    active: Optional[bool] = None


class AiSettingsUpdateRequest(BaseModel):
    provider: str
    model: str
    apiKey: Optional[str] = None


class AiSettingsResponse(BaseModel):
    provider: str
    model: str
    apiKeyConfigured: bool


class AiSettingsTestResponse(BaseModel):
    success: bool
    message: str


class GenerateWorkflowRequest(BaseModel):
    businessName: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=4000)
    supportedNodeTypes: List[str] = Field(default_factory=list)


class GenerateWorkflowResponse(BaseModel):
    workflow: Dict[str, Any]
    description: Optional[str] = None


class WorkflowResponse(BaseModel):
    id: str
    name: str
    businessName: str
    description: str
    category: str
    active: bool
    createdAt: str
    updatedAt: str
    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    n8nId: Optional[str] = None
    stats: Optional[Dict[str, Any]] = None
