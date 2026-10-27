# import db_tinydb as db
import pytest
from tinydb import where
from main import load_all_inputs
import db.db_tinydb as db
from clinic_manager import ClinicManager
from services.patient_department_request_service import DepartmentPatientRequestService
from .config import generate_requests


@pytest.fixture(autouse=True)
def init_db():
    db.clinic.drop_tables()

    if generate_requests:
        db.init_db()

        closed_pat_requests = db.patient_requests.search(
            where('status') == "Closed")
        assert len(closed_pat_requests) == 100, "Db init with 100 closed worked"

        open_pat_requests = db.patient_requests.search(
            where('status') == "Open")
        assert len(
            open_pat_requests) == 0, "DB has open request before processing"


def test_task_processing_with_department_support():

    inputs = load_all_inputs()

    patient_request_manger = ClinicManager(DepartmentPatientRequestService())

    first_update(inputs, patient_request_manger)

    second_update(inputs, patient_request_manger)

    third_update(inputs, patient_request_manger)

    fourth_update(inputs, patient_request_manger)


def first_update(inputs, patient_request_manger):
    patient_request_manger.process_tasks_update(inputs[0])

    patient1_open = get_open_patient_requests("patient1")

    assert len(
        patient1_open) == 1, "Error after input 1, incorrect number of open requests"

    pat1_req = patient1_open[0]

    assert len(
        pat1_req['messages']) == 2, "Error after input 1, messages length on the open request is incorrect "
    assert set(map(lambda m: m['code'], pat1_req['medications'])) == {"ACET001", "IBU001", "LISI001"
                                                                      }, "Error after input 1,  the medication set is incorrect"

    patient2_open = get_open_patient_requests("patient2")

    assert len(
        patient2_open) == 2, "Error after input 1, incorrect number of open requests.\
            Patient 2 tasks should have been split to two requests due to department grouping"

    pat2_req = next(
        (req for req in patient2_open if req['assigned_to'] == 'Dermatology'), None)

    assert len(
        pat2_req['messages']) == 1, "Error after input 1, messages length on the open request is incorrect "
    assert set(map(lambda m: m['code'], pat2_req['medications'])) == {"METR001", "IBU001"
                                                                      }, "Error after input 1,  the medication set is incorrect for patient2"

    assert len(
        get_open_patient_requests("patient3")) == 2, "Error after input 1, incorrect number of open requests.\
            Patient 3 tasks should have been split to two requests due to department grouping"


def second_update(inputs, patient_request_manger):
    patient_request_manger.process_tasks_update(inputs[1])
    assert len(get_open_patient_requests(
        "patient1")) == 0, "Error after input 2, the patient1 request should be closed"

    patient2_open = get_open_patient_requests("patient2")
    assert len(patient2_open) == 2, "Error after input 2"

    pat2_req = next(
        (req for req in patient2_open if req['assigned_to'] == 'Primary'), None)

    assert pat2_req['messages'] == [
        "The results of the patient's blood tests are in. Please review them ASAP."
    ], "Error after input 2, "

    assert len(
        get_open_patient_requests("patient3")) == 1, "Error after input 1, incorrect number of open requests.\
            Patient 3 tasks should have been merged to a single department grouping"


def third_update(inputs, patient_request_manger):
    patient_request_manger.process_tasks_update(inputs[2])

    assert count_open_patient_requests(
    ) == 0, "Error after input 3, there should be no open requests left"


def fourth_update(inputs, patient_request_manger):
    patient_request_manger.process_tasks_update(inputs[3])
    assert count_open_patient_requests(
    ) == 1, "Error after input 4, there should be one open request"


def count_open_patient_requests():
    return len(db.patient_requests.search(where('status') == "Open"))


def get_open_patient_requests(patient_id) -> list[dict] | None:
    return db.patient_requests.search(
        (where('patient_id') == patient_id) & (where('status') == 'Open'))
