from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field

TaskEventReason = Literal['created', 'moved', 'closed']


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskEvent(BaseModel):
    """ Audit record of a task entering, moving between, or leaving open requests.

    `created`: entered an open request. `moved`: switched to another open request (department change).
    `closed`: task closed, it left its open request or never had one.
    """
    patient_id: str
    task_id: str
    reason: TaskEventReason
    from_request_id: Optional[str] = None
    to_request_id: Optional[str] = None
    from_department: Optional[str] = None
    to_department: Optional[str] = None
    timestamp: datetime = Field(default_factory=_utc_now)
