from pydantic import Field, BaseModel
from .patient_task import PatientTask


class TaskInput(BaseModel):
    tasks: list[PatientTask] = Field(default_factory=list)
