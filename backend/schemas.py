from typing import Any
from pydantic import BaseModel, Field


class ExecuteRequest(BaseModel):
    prompt: str = Field(min_length=2)


class IntentPayload(BaseModel):
    platform: str
    action: str
    intent: str
    parameters: dict[str, Any]


class QueryResultPayload(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


class ExecuteResponse(BaseModel):
    intent: IntentPayload
    sql: str
    result: QueryResultPayload
    message: str
    model: str
    connector: str
    mcp_validation: str
    execution_time_sec: float
    security_checks: list[str]
