from collections import defaultdict
from datetime import datetime, timezone

from tinydb import where

import db.db_tinydb as db
from models.patient_request import PatientRequest
from models.patient_task import PatientTask
from models.task_event import TaskEvent
from .abstract_patient_request_service import PatientRequestService
from .task_event_service import (
    RequestSlot, TaskEventService, TaskMembership, build_task_events)
from .task_service import TaskService

TasksByDepartment = dict[str, list[PatientTask]]
PatientToTasksByDepartment = dict[str, TasksByDepartment]

RequestsByDepartment = dict[str, PatientRequest]
PatientToRequestsByDepartment = dict[str, RequestsByDepartment]


class DepartmentPatientRequestService(PatientRequestService):
    """ Groups tasks into requests by (patient id, department), where the department is `assigned_to`.

    Invariant: a patient has at most one open request per department. A closed request is never reopened,
    a later task for that department gets a new request.

    Instead of patching requests when a task moves, every update reconciles the touched patients: it regroups
    their open tasks by department and compares the groups with their open requests. Requests whose group
    still exists are rebuilt, new groups get a request, and requests whose group is empty are closed.
    """

    def __init__(self, task_service: TaskService | None = None,
                 event_service: TaskEventService | None = None):
        super().__init__(task_service)
        self.event_service = event_service or TaskEventService()

    def update_requests(self, changed_tasks: list[PatientTask]):
        if not changed_tasks:
            return

        patient_ids = {t.patient_id for t in changed_tasks}

        tasks_by_patient = self._load_tasks_by_patient(
            patient_ids, changed_tasks)
        requests_by_patient = self._load_open_requests_by_patient(patient_ids)

        now = datetime.now(timezone.utc)
        events: list[TaskEvent] = []

        for patient_id in sorted(patient_ids):
            patient_changes = [
                t for t in changed_tasks if t.patient_id == patient_id]
            events += self._reconcile_patient(
                patient_id,
                tasks_by_patient[patient_id],
                requests_by_patient[patient_id],
                last_change=max(t.updated_date for t in patient_changes),
                now=now)

        # The events are written after the requests. They are a log, so a failure here never leaves a request
        # in a wrong state. In a real DB the writes would share a transaction.
        self.event_service.record(events)

    def _load_tasks_by_patient(self, patient_ids: set[str], changed_tasks: list[PatientTask]
                               ) -> PatientToTasksByDepartment:
        """ Groups the touched patients' open tasks and the tasks that were closed by this update. """
        newly_closed = [t for t in changed_tasks if t.status == 'Closed']
        open_tasks = self.task_service.get_open_tasks_for_patients(patient_ids)

        grouped: PatientToTasksByDepartment = defaultdict(
            lambda: defaultdict(list))
        for task in [*open_tasks, *newly_closed]:
            grouped[task.patient_id][task.assigned_to].append(task)
        return grouped

    def _load_open_requests_by_patient(self, patient_ids: set[str]) -> PatientToRequestsByDepartment:
        open_docs = db.patient_requests.search(
            where('patient_id').one_of(list(patient_ids)) & (where('status') == 'Open'))

        grouped: PatientToRequestsByDepartment = defaultdict(dict)
        for doc in open_docs:
            request = PatientRequest(**doc)
            if request.assigned_to in grouped[request.patient_id]:
                raise ValueError(
                    f'Patient {request.patient_id} has more than one open request for {request.assigned_to}')
            grouped[request.patient_id][request.assigned_to] = request
        return grouped

    def _reconcile_patient(self, patient_id: str, tasks_by_department: TasksByDepartment,
                           open_requests: RequestsByDepartment, last_change: datetime,
                           now: datetime) -> list[TaskEvent]:
        old_membership = self._membership(open_requests.values())
        new_open_requests: list[PatientRequest] = []
        emptied_requests: list[RequestSlot] = []

        for department in sorted(tasks_by_department.keys() | open_requests.keys()):
            tasks = tasks_by_department.get(department, [])
            existing = open_requests.get(department)

            if any(t.status == 'Open' for t in tasks):
                request = self.to_patient_request(patient_id, tasks)
                if existing:
                    request.id = existing.id
                    db.patient_requests.update(
                        request.model_dump(), where('id') == existing.id)
                else:
                    db.patient_requests.insert(request.model_dump())
                new_open_requests.append(request)

            elif existing:
                closed = self._close_request(existing, tasks, last_change)
                db.patient_requests.update(
                    closed.model_dump(), where('id') == existing.id)
                if not tasks:
                    emptied_requests.append(
                        RequestSlot(existing.id, existing.assigned_to))

            # Otherwise the department only has closed tasks and no open request: nothing to open or close.

        task_departments = {
            t.id: t.assigned_to for tasks in tasks_by_department.values() for t in tasks}

        return build_task_events(
            patient_id, old_membership, self._membership(new_open_requests),
            task_departments, emptied_requests, now)

    @staticmethod
    def _close_request(request: PatientRequest, closed_tasks: list[PatientTask],
                       last_change: datetime) -> PatientRequest:
        """ Closes a request and clears its data, the task events keep the record of what happened to its tasks.
        `closed_tasks` are the tasks that were closed in it by this update, empty if its tasks all moved away. """
        return request.model_copy(update={
            'status': 'Closed',
            'messages': [],
            'medications': [],
            'task_ids': {t.id for t in closed_tasks},
            'updated_date': max(request.updated_date, last_change),
        })

    @staticmethod
    def _membership(requests) -> TaskMembership:
        return {task_id: RequestSlot(request.id, request.assigned_to)
                for request in requests for task_id in request.task_ids}
