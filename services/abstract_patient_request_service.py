from abc import ABC, abstractmethod
from models.patient_task import PatientTask
from uuid import uuid4

from models.patient_request import PatientRequest
from .task_service import TaskService
from operator import attrgetter

task_date_getter = attrgetter('updated_date')


class PatientRequestService(ABC):

    def __init__(self, task_service: TaskService | None = None):
        self.task_service = task_service or TaskService()

    @abstractmethod
    def update_requests(self, changed_tasks: list[PatientTask]):
        """ Updates the patient requests for the changed tasks (already stored). Loads any other tasks it needs. """
        raise NotImplementedError

    def to_patient_request(self, patient_id, patient_tasks):

        open_tasks: list[PatientTask] = [
            t for t in patient_tasks if t.status == 'Open']

        req_status = 'Open' if len(open_tasks) > 0 else 'Closed'

        # We only care about the closed tasks if all the tasks are closed and we are closing the request.
        req_tasks = open_tasks or patient_tasks

        tasks_by_updated_asc: list[PatientTask] = sorted(
            req_tasks, key=task_date_getter)

        newest_task = tasks_by_updated_asc[-1]
        oldest_created_date = min(
            (task.created_date for task in req_tasks))
        new_pat_req = PatientRequest(
            id=str(uuid4()),
            assigned_to=newest_task.assigned_to,
            created_date=oldest_created_date,
            updated_date=newest_task.updated_date,
            patient_id=patient_id,
            pharmacy_id=newest_task.pharmacy_id,
            task_ids={t.id for t in req_tasks},
            status=req_status,
            messages=[
                t.message for t in tasks_by_updated_asc],
            medications={
                m for t in req_tasks for m in t.medications}
        )

        return new_pat_req
