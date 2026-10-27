from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel

TaskEventReason = Literal['created', 'moved', 'closed', 'request_closed_empty']


class TaskEvent(BaseModel):
    """ An append-only audit record of a task entering, moving between, or leaving patient requests.

    `created`: the task entered an open request for the first time.
    `moved`: the task left one open request and entered another (a department change).
    `closed`: the task was closed and left its open request.
    `request_closed_empty`: a request was closed because all of its tasks left the department.
        It is a request level event, so `task_id` is empty.
    """
    patient_id: str
    reason: TaskEventReason
    timestamp: datetime
    task_id: Optional[str] = None
    from_request_id: Optional[str] = None
    to_request_id: Optional[str] = None
    from_department: Optional[str] = None
    to_department: Optional[str] = None
