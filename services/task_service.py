from collections.abc import Iterable
from tinydb import Query, where
from models.patient_task import PatientTask
from models.patient_request import PatientRequest
import db.db_tinydb as db

from operator import attrgetter

task_date_getter = attrgetter('updated_date')

Task = Query()


class TaskService:

    def updates_tasks(self, tasks: list[PatientTask]):
        # Question : This code is the result of a limitation by TinyDB. What is the issue and what feature
        # would a more complete DB solution offer ?
        for task in tasks:
            db.tasks.upsert(task.model_dump(), Task.id == task.id)

    def get_open_tasks(self) -> Iterable[PatientTask]:
        return (PatientTask(**task_doc) for task_doc in db.tasks.search(where("status") == 'Open'))
