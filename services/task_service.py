from collections.abc import Iterable
from tinydb import Query, where
from models.patient_task import PatientTask
import db.db_tinydb as db

Task = Query()


class TaskService:

    def updates_tasks(self, tasks: list[PatientTask]):
        # Question : This code is the result of a limitation by TinyDB. What is the issue and what feature
        # would a more complete DB solution offer ?
        # Answer: TinyDB has no bulk upsert, so every task costs a separate lookup and write (N round trips),
        # (and without an index on the task id each lookup also scans the whole tasks table, so the cost is
        # about N x table size), and there is no transaction, so a failure half way leaves the batch partially applied. A DB such as
        # MongoDB offers bulkWrite with upsert (one round trip), a unique index on the task id that makes the
        # upsert atomic, and multi-document transactions for the task, request and event writes.
        for task in tasks:
            db.tasks.upsert(task.model_dump(), Task.id == task.id)

    def get_open_tasks(self) -> Iterable[PatientTask]:
        return (PatientTask(**task_doc) for task_doc in db.tasks.search(where("status") == 'Open'))

    def get_open_tasks_for_patients(self, patient_ids: Iterable[str]) -> Iterable[PatientTask]:
        """ Returns the open tasks of the given patients only, instead of scanning every open task. """
        patient_ids = list(patient_ids)
        return (PatientTask(**task_doc) for task_doc in db.tasks.search(
            (where("patient_id").one_of(patient_ids)) & (where("status") == 'Open')))
