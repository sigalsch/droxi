from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field
from .patient_task import Medication


class PatientRequest(BaseModel):
    id: str
    patient_id: str
    status: Literal['Open'] | Literal['Closed']
    assigned_to: str
    created_date: datetime
    updated_date: datetime
    messages: list[str]
    medications: list[Medication] = Field(default_factory=list)
    pharmacy_id: Optional[int]

    task_ids: set[str]

    # NOTE: Model can be extended as desired
