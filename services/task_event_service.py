import logging

import db.db_tinydb as db
from models.patient_request import PatientRequest
from models.patient_task import PatientTask
from models.task_event import TaskEvent

logger = logging.getLogger(__name__)


def build_task_events(changed_tasks: list[PatientTask], open_before: list[PatientRequest],
                      open_after: list[PatientRequest]) -> list[TaskEvent]:
    """ One event per changed task whose open request differs before and after the update.
    A task that arrives already closed is recorded as `closed` without requests. """
    holder_before = _request_by_task_id(open_before)
    holder_after = _request_by_task_id(open_after)

    events: list[TaskEvent] = []
    for task in sorted({t.id: t for t in changed_tasks}.values(), key=lambda t: t.id):
        before = holder_before.get(task.id)
        after = holder_after.get(task.id)

        if before and after and before.id == after.id:
            continue

        reason = 'created' if after and not before else 'closed' if not after else 'moved'
        events.append(TaskEvent(
            patient_id=task.patient_id,
            task_id=task.id,
            reason=reason,
            from_request_id=before.id if before else None,
            to_request_id=after.id if after else None,
            from_department=before.assigned_to if before else None,
            to_department=after.assigned_to if after else None,
        ))
    return events


def _request_by_task_id(requests: list[PatientRequest]) -> dict[str, PatientRequest]:
    return {task_id: request for request in requests for task_id in request.task_ids}


class TaskEventService:
    """ Writes the append-only task event log. """

    def record(self, events: list[TaskEvent]):
        if not events:
            return
        for e in events:
            logger.debug('task %s %s: request %s (%s) -> %s (%s)', e.task_id, e.reason,
                         e.from_request_id, e.from_department, e.to_request_id, e.to_department)
        db.task_events.insert_multiple([e.model_dump() for e in events])
