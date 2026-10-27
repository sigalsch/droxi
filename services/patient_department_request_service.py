import logging
from collections import Counter, defaultdict
from time import perf_counter

from tinydb import where

import db.db_tinydb as db
from models.patient_request import PatientRequest
from models.patient_task import PatientTask
from .abstract_patient_request_service import PatientRequestService
from .task_event_service import TaskEventService, build_task_events
from .task_service import TaskService

logger = logging.getLogger(__name__)

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

        started = perf_counter()
        patient_ids = {t.patient_id for t in changed_tasks}
        tasks_by_group = self._load_tasks_by_group(patient_ids, changed_tasks)
        open_requests = self._load_open_requests_by_group(patient_ids)

        actions: Counter[str] = Counter()
        open_after: list[PatientRequest] = []
        for group in sorted(tasks_by_group.keys() | open_requests.keys()):
            request, action = self._reconcile_group(
                group, tasks_by_group.get(group, []), open_requests.get(group))
            actions[action] += 1
            if request:
                open_after.append(request)

        # Events are written last, so a failure cannot leave a request wrong. A real DB would use one transaction.
        events = build_task_events(
            changed_tasks, list(open_requests.values()), open_after)
        self.event_service.record(events)

        event_reasons = Counter(e.reason for e in events)
        logger.info(
            'requests updated: changed_tasks=%d patients=%d created=%d rebuilt=%d closed=%d '
            'events=%d (created=%d moved=%d closed=%d) duration_ms=%.1f',
            len(changed_tasks), len(patient_ids), actions['created'], actions['rebuilt'], actions['closed'],
            len(events), event_reasons['created'], event_reasons['moved'], event_reasons['closed'],
            (perf_counter() - started) * 1000)

    def _load_tasks_by_group(self, patient_ids: set[str], changed_tasks: list[PatientTask]
                             ) -> dict[GroupKey, list[PatientTask]]:
        """ Groups the touched patients' open tasks with the tasks closed by this update. """
        newly_closed = [t for t in changed_tasks if t.status == 'Closed']
        open_tasks = list(
            self.task_service.get_open_tasks_for_patients(patient_ids))
        logger.debug('loaded %d open tasks for %d patients, %d tasks closed by this update',
                     len(open_tasks), len(patient_ids), len(newly_closed))

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
        logger.debug('loaded %d open requests for %d patients',
                     len(grouped), len(patient_ids))
        return grouped

    def _reconcile_group(self, group: GroupKey, tasks: list[PatientTask],
                         existing: PatientRequest | None) -> tuple[PatientRequest | None, str]:
        """ Syncs the request of one group with its tasks.
        Returns the resulting open request (or None) and the action taken:
        'created', 'rebuilt', 'closed' or 'none'. """
        patient_id, department = group

        if any(t.status == 'Open' for t in tasks):
            request = self.to_patient_request(patient_id, tasks)
            if existing:
                request.id = existing.id
            self._save(request)
            action = 'rebuilt' if existing else 'created'
            logger.debug('%s request %s for %s/%s from %d tasks',
                         action, request.id, patient_id, department, len(tasks))
            return request, action

        if existing:
            self._save(self._close_request(existing, tasks))
            logger.debug('closed request %s for %s/%s, %d tasks closed in it',
                         existing.id, patient_id, department, len(tasks))
            return None, 'closed'

        return None, 'none'

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
