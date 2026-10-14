from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class SimulationCriticalRequest(BaseModel):
    zone_id: int = Field(ge=1, le=4)


class SimulationCriticalBatchRequest(BaseModel):
    zone_ids: list[int] = Field(min_length=1, max_length=4)

    @field_validator("zone_ids")
    @classmethod
    def zone_ids_must_be_unique(cls, values: list[int]) -> list[int]:
        if len(set(values)) != len(values):
            raise ValueError("zone_ids must not contain duplicates")
        return values


class ThresholdUpdate(BaseModel):
    warning_threshold: float = Field(gt=0)
    critical_threshold: float = Field(gt=0)


class ActorRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class Zone(BaseModel):
    id: int
    name: str
    gas_name: str
    formula: str
    unit: str
    warning_threshold: float
    critical_threshold: float
    current_reading: float | None = None
    previous_reading: float | None = None
    status: Literal["NORMAL", "WARNING", "CRITICAL"] = "NORMAL"
    last_updated: datetime | None = None
    active_incident_id: str | None = None


def classify(value: float, warning: float, critical: float) -> str:
    if value >= critical:
        return "CRITICAL"
    if value >= warning:
        return "WARNING"
    return "NORMAL"
