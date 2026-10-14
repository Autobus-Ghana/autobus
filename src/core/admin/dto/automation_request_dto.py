from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field


class AutomationRequestResponse(BaseModel):
    """Automation request response matching the admin portal schema."""
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str
    title: str
    description: str
    category: str
    priority: str
    status: str
    requested_by: str = Field(alias="requestedBy")
    created_at: str = Field(alias="createdAt")
    assigned_to: Optional[str] = Field(default=None, alias="assignedTo")
    workflow_id: Optional[str] = Field(default=None, alias="workflowId")
    business_name: Optional[str] = Field(default=None, alias="businessName")
    contact_email: Optional[str] = Field(default=None, alias="contactEmail")
    contact_phone: Optional[str] = Field(default=None, alias="contactPhone")
    survey_data: Optional[Dict[str, Any]] = Field(default=None, alias="surveyData")
    user_id: Optional[str] = Field(default=None, alias="userId")


class CreateAutomationRequestPayload(BaseModel):
    """Payload to create an automation request from admin portal or API."""
    model_config = ConfigDict(populate_by_name=True)

    title: Optional[str] = None
    description: Optional[str] = None
    category: str = "Other"
    priority: str = "Normal"
    requested_by: Optional[str] = Field(default=None, alias="requestedBy")
    business_name: Optional[str] = Field(default=None, alias="businessName")
    contact_email: Optional[str] = Field(default=None, alias="contactEmail")
    contact_phone: Optional[str] = Field(default=None, alias="contactPhone")
    survey_data: Optional[Dict[str, Any]] = Field(default=None, alias="surveyData")
    answers: Optional[Dict[str, Any]] = None


class UpdateAutomationRequestStatusPayload(BaseModel):
    """Payload to update the triage/delivery status of an automation request."""
    status: str


class PortalSurveySubmitRequest(BaseModel):
    """Payload for submitting automation survey details from the Autobus portal."""
    model_config = ConfigDict(populate_by_name=True)

    business_name: Optional[str] = Field(default=None, alias="businessName")
    contact_name: Optional[str] = Field(default=None, alias="contactName")
    contact_email: Optional[str] = Field(default=None, alias="contactEmail")
    contact_phone: Optional[str] = Field(default=None, alias="contactPhone")
    category: Optional[str] = "Other"
    priority: Optional[str] = "Normal"
    title: Optional[str] = None
    description: Optional[str] = None
    answers: Optional[Dict[str, Any]] = Field(default_factory=dict)
    survey_data: Optional[Dict[str, Any]] = Field(default=None, alias="surveyData")
