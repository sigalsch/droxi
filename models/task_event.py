from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field

TaskEventReason = Literal['created', 'moved', 'closed']


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskEvent(BaseModel):
    """ An append-only audit record of a task entering, moving between, or leaving open patient requests.

    `created`: the task entered an open request.
    `moved`: the task left one open request and entered another (a department change).
    `closed`: the task was closed. It left its open request, or it was never in one.
    """
    patient_id: str
    task_id: str
    reason: TaskEventReason
    from_request_id: Optional[str] = None
    to_request_id: Optional[str] = None
    from_department: Optional[str] = None
    to_department: Optional[str] = None
    timestamp: datetime = Field(default_factory=_utc_now)
