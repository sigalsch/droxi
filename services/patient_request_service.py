from collections import defaultdict

from uuid import uuid4

from tinydb import where
from models.patient_task import PatientTask
from models.patient_request import PatientRequest
from .abstract_patient_request_service import PatientRequestService
import db.db_tinydb as db

from operator import attrgetter

task_date_getter = attrgetter('updated_date')


class PerPatientRequestService(PatientRequestService):

    def get_open_patient_request(self, patient_id) -> PatientRequest | None:
        result_dict = db.patient_requests.get(
            (where('patient_id') == patient_id) & (where('status') == 'Open'))

        if not result_dict:
            return None

        return PatientRequest(**result_dict)

    def update_requests(self, tasks: list[PatientTask]):

        grouped_by_patent: dict[str, list[PatientTask]] = defaultdict(list)

        for task in tasks:
            grouped_by_patent[task.patient_id].append(task)

        for patient_id, patient_tasks in grouped_by_patent.items():

            existing_req: PatientRequest = self.get_open_patient_request(
                patient_id)

            pat_req = self.to_patient_request(patient_id, patient_tasks)

            if not existing_req:
                db.patient_requests.insert(pat_req.model_dump())
            else:
                pat_req.id = existing_req.id
                db.patient_requests.update(
                    pat_req.model_dump(), where('id') == existing_req.id)
