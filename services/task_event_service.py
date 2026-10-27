from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

import db.db_tinydb as db
from models.task_event import TaskEvent


@dataclass(frozen=True)
class RequestSlot:
    """ The open request (and its department) that currently holds a task. """
    request_id: str
    department: str


# task id -> the open request that holds it. Tasks that are not in an open request are absent.
TaskMembership = dict[str, RequestSlot]


def build_task_events(
    patient_id: str,
    old_membership: TaskMembership,
    new_membership: TaskMembership,
    task_departments: Mapping[str, str],
    emptied_requests: Iterable[RequestSlot],
    timestamp: datetime,
) -> list[TaskEvent]:
    """ Derives the audit events by comparing which open request held each task before and after an update.

    `task_departments` holds the current department of the tasks in the update. It is used to report where a
    task that was closed ended up, since a closed task is no longer in any open request.
    """
    events: list[TaskEvent] = []

    for task_id in sorted(old_membership.keys() | new_membership.keys()):
        before = old_membership.get(task_id)
        after = new_membership.get(task_id)

        if before == after:
            continue

        if before is None:
            reason = 'created'
        elif after is None:
            reason = 'closed'
        else:
            reason = 'moved'

        events.append(TaskEvent(
            patient_id=patient_id,
            task_id=task_id,
            reason=reason,
            timestamp=timestamp,
            from_request_id=before.request_id if before else None,
            to_request_id=after.request_id if after else None,
            from_department=before.department if before else None,
            to_department=after.department if after else task_departments.get(
                task_id),
        ))

    for emptied in emptied_requests:
        events.append(TaskEvent(
            patient_id=patient_id,
            reason='request_closed_empty',
            timestamp=timestamp,
            from_request_id=emptied.request_id,
            from_department=emptied.department,
        ))

    return events


class TaskEventService:
    """ Writes the append-only task event log. Nothing in the request flow reads it back. """

    def record(self, events: list[TaskEvent]):
        if not events:
            return
        db.task_events.insert_multiple([e.model_dump() for e in events])
