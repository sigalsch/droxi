from os import PathLike
from pathlib import Path
from models import TaskInput
from clinic_manager import ClinicManager
from observability import configure_logging
from services.patient_department_request_service import DepartmentPatientRequestService
import db.db_tinydb as db


cur_dir = Path(__file__).parent

files = ['./data/input_task_1.json',
         './data/input_task_2.json', './data/input_task_3.json', './data/input_task_4.json']


def load_input(file: PathLike):
    with open(file) as f:
        input_json = f.read()

    task_input = TaskInput.model_validate_json(input_json)
    return task_input


def load_all_inputs():
    return [load_input(cur_dir / f) for f in files]


def main():
    configure_logging()
    db.init_db()
    inputs = load_all_inputs()

    manager = ClinicManager(DepartmentPatientRequestService())
    for task_input in inputs:
        manager.process_tasks_update(task_input)


if __name__ == '__main__':
    main()
