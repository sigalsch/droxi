
import logging
from time import perf_counter

from models import TaskInput
from observability import elapsed_ms
from services.patient_request_service import PerPatientRequestService
from services.abstract_patient_request_service import PatientRequestService
from services.task_service import TaskService

logger = logging.getLogger(__name__)


class ClinicManager:

    def __init__(self, patientRequestService: PatientRequestService | None = None):
        self.task_service = TaskService()
        self.patient_request_service = patientRequestService or PerPatientRequestService(
            self.task_service)

    def process_tasks_update(self, task_input: TaskInput):
        """Accepts a task_input object that contains all the tasks that were modified since
        the last time this method was called. The method process the changes to the tasks, and updates the patient requests appropriately. """

        tasks = task_input.tasks
        if not tasks:
            logger.debug('empty tasks update, nothing to do')
            return

        started = perf_counter()
        stage = 'storing tasks'
        # The error is logged with its type only, its message may hold task data (PHI rule).
        # Safe for the caller to retry: storing tasks and updating requests are both idempotent.
        try:
            self.task_service.updates_tasks(tasks)
            logger.debug('stored %d tasks in %.1f ms', len(tasks), elapsed_ms(started))

            # Previous code:
            #
            # newly_closed_tasks = [t for t in tasks if t.status == 'Closed']
            #
            # Question: What is a potential performance issue with this code ?
            # open_tasks = list(self.task_service.get_open_tasks())
            #
            # self.patient_request_service.update_requests(
            #     open_tasks + newly_closed_tasks)
            #
            # Answer: get_open_tasks() loads every open task of every patient on each call, although an update
            # only touches dozens of tasks and only thousands of the millions of patients have open requests.
            # The cost grows with the whole open backlog instead of with the size of the update, and the
            # service gets a list that does not say which patients were touched, so it cannot scope its work.
            #
            # Now the service receives only the changed tasks, and loads the other tasks it needs itself.
            # This lets each service scope its reads to the patients touched by this update.
            stage = 'updating requests'
            self.patient_request_service.update_requests(tasks)
            logger.debug('tasks update processed in %.1f ms', elapsed_ms(started))
        except Exception as error:
            logger.error('tasks update failed while %s: tasks=%d error=%s',
                         stage, len(tasks), type(error).__name__)
            raise
