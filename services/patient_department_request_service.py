
from models.patient_task import PatientTask
from .abstract_patient_request_service import PatientRequestService
from typing import NewType

DepartmentToPatientTasks = NewType(
    'DepartmentToPatientTasks', dict[str, list[PatientTask]])

PatientToGroupedDepartmentTasks = dict[str, DepartmentToPatientTasks]


class DepartmentPatientRequestService(PatientRequestService):

    def update_requests(self, tasks: list[PatientTask]):
        raise NotImplementedError
