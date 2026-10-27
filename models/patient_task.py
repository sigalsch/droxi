from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


class Medication(BaseModel, frozen=True):
    code: str
    name: str



class PatientTask(BaseModel):
    id: str
    patient_id: str
    status: Literal['Open'] | Literal['Closed']
    assigned_to: str
    created_date: datetime
    updated_date: datetime
    message: str
    medications: list[Medication] = Field(default_factory=list)
    pharmacy_id: Optional[int] = None
