from collections import defaultdict

from tinydb import where

import db.db_tinydb as db
from models.patient_request import PatientRequest
from models.patient_task import PatientTask
from .abstract_patient_request_service import PatientRequestService
from .task_event_service import TaskEventService, build_task_events
from .task_service import TaskService

# A request holds the tasks of one patient in one department: (patient_id, department)
GroupKey = tuple[str, str]


class DepartmentPatientRequestService(PatientRequestService):
    """ Groups tasks into requests by (patient id, department), the department being `assigned_to`.

    Each update reconciles the touched patients: their tasks are regrouped by department and compared with
    their open requests. A group with open tasks gets its request rebuilt or created, a request without open
    tasks is closed. A patient has at most one open request per department, and a closed one is never reopened.
    """

    def __init__(self, task_service: TaskService | None = None,
                 event_service: TaskEventService | None = None):
        super().__init__(task_service)
        self.event_service = event_service or TaskEventService()

    def update_requests(self, changed_tasks: list[PatientTask]):
        if not changed_tasks:
            return

        patient_ids = {t.patient_id for t in changed_tasks}
        tasks_by_group = self._load_tasks_by_group(patient_ids, changed_tasks)
        open_requests = self._load_open_requests_by_group(patient_ids)

        open_after: list[PatientRequest] = []
        for group in sorted(tasks_by_group.keys() | open_requests.keys()):
            request = self._reconcile_group(
                group, tasks_by_group.get(group, []), open_requests.get(group))
            if request:
                open_after.append(request)

        # Events are written last, so a failure cannot leave a request wrong. A real DB would use one transaction.
        self.event_service.record(
            build_task_events(changed_tasks, list(open_requests.values()), open_after))

    def _load_tasks_by_group(self, patient_ids: set[str], changed_tasks: list[PatientTask]
                             ) -> dict[GroupKey, list[PatientTask]]:
        """ Groups the touched patients' open tasks with the tasks closed by this update. """
        newly_closed = [t for t in changed_tasks if t.status == 'Closed']
        open_tasks = self.task_service.get_open_tasks_for_patients(patient_ids)

        grouped: dict[GroupKey, list[PatientTask]] = defaultdict(list)
        for task in [*open_tasks, *newly_closed]:
            grouped[(task.patient_id, task.assigned_to)].append(task)
        return grouped

    def _load_open_requests_by_group(self, patient_ids: set[str]) -> dict[GroupKey, PatientRequest]:
        open_docs = db.patient_requests.search(
            where('patient_id').one_of(list(patient_ids)) & (where('status') == 'Open'))

        grouped: dict[GroupKey, PatientRequest] = {}
        for doc in open_docs:
            request = PatientRequest(**doc)
            group = (request.patient_id, request.assigned_to)
            if group in grouped:
                raise ValueError(
                    f'Patient {group[0]} has more than one open request for {group[1]}')
            grouped[group] = request
        return grouped

    def _reconcile_group(self, group: GroupKey, tasks: list[PatientTask],
                         existing: PatientRequest | None) -> PatientRequest | None:
        """ Syncs the request of one group with its tasks. Returns the resulting open request, or None. """
        patient_id, _ = group

        if any(t.status == 'Open' for t in tasks):
            request = self.to_patient_request(patient_id, tasks)
            if existing:
                request.id = existing.id
            self._save(request)
            return request

        if existing:
            self._save(self._close_request(existing, tasks))

        return None

    @staticmethod
    def _close_request(request: PatientRequest, closed_tasks: list[PatientTask]) -> PatientRequest:
        """ Closes a request and clears its data. `closed_tasks` are those closed in it by this update,
        empty if its tasks all moved away. """
        return request.model_copy(update={
            'status': 'Closed',
            'messages': [],
            'medications': [],
            'task_ids': {t.id for t in closed_tasks},
            'updated_date': max([request.updated_date, *(t.updated_date for t in closed_tasks)]),
        })

    @staticmethod
    def _save(request: PatientRequest):
        db.patient_requests.upsert(
            request.model_dump(), where('id') == request.id)
