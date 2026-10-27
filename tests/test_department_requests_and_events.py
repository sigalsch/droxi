import logging
from datetime import datetime, timezone

import pytest
from tinydb import where

import db.db_tinydb as db
from clinic_manager import ClinicManager
from main import load_all_inputs
from models import PatientRequest, PatientTask, TaskInput
from services.patient_department_request_service import (
    DepartmentPatientRequestService, DuplicateOpenRequestError)
from services.patient_request_service import PerPatientRequestService
from services.task_event_service import build_task_events
from services.task_service import TaskService


@pytest.fixture(autouse=True)
def empty_db():
    db.clinic.drop_tables()
    # drop_tables leaves the module level tables with their query cache from the previous test
    for table in (db.patient_requests, db.tasks, db.task_events):
        table.clear_cache()


@pytest.fixture
def inputs():
    return load_all_inputs()


@pytest.fixture
def manager():
    return ClinicManager(DepartmentPatientRequestService())


def requests_of(patient_id, status=None):
    condition = where('patient_id') == patient_id
    if status:
        condition &= where('status') == status
    return db.patient_requests.search(condition)


def request_for(patient_id, department, status):
    matches = [r for r in requests_of(patient_id, status)
               if r['assigned_to'] == department]
    assert len(matches) == 1
    return matches[0]


def events_of(**fields):
    return [e for e in db.task_events.all()
            if all(e[name] == value for name, value in fields.items())]


def test_task_moved_between_departments_is_recorded(manager, inputs):
    manager.process_tasks_update(inputs[0])
    radiology_id = request_for('patient3', 'Radiology', 'Open')['id']

    manager.process_tasks_update(inputs[1])
    primary_id = request_for('patient3', 'Primary', 'Open')['id']

    (moved,) = events_of(task_id='task3', reason='moved')
    assert moved['from_request_id'] == radiology_id
    assert moved['to_request_id'] == primary_id
    assert (moved['from_department'], moved['to_department']
            ) == ('Radiology', 'Primary')


def test_request_emptied_by_a_move_is_closed_and_cleared(manager, inputs):
    """ Its tasks are open in another department, so their data is not copied into the closed request """
    manager.process_tasks_update(inputs[0])
    radiology_id = request_for('patient3', 'Radiology', 'Open')['id']

    manager.process_tasks_update(inputs[1])

    closed = request_for('patient3', 'Radiology', 'Closed')
    assert closed['id'] == radiology_id
    assert closed['messages'] == []
    assert closed['medications'] == []
    assert not closed['task_ids']
    assert events_of(task_id='task3', reason='moved')[
        0]['from_request_id'] == radiology_id


def test_closing_a_request_keeps_the_data_of_the_tasks_closed_in_it(manager, inputs):
    """ Same as `to_patient_request`: a closed request is medical history """
    manager.process_tasks_update(inputs[0])
    open_id = request_for('patient1', 'Primary', 'Open')['id']
    manager.process_tasks_update(inputs[1])

    closed = request_for('patient1', 'Primary', 'Closed')

    assert closed['id'] == open_id
    assert set(closed['task_ids']) == {'task1', 'task4'}
    assert sorted(closed['messages']) == [
        'Patient needs follow-up appointment', 'Patient needs medication refill']
    assert {m['code'] for m in closed['medications']
            } == {'ACET001', 'IBU001', 'LISI001'}
    assert {e['reason'] for e in events_of(patient_id='patient1')} == {
        'created', 'closed'}


def test_closing_request_matches_the_closed_request_of_the_per_patient_service(inputs):
    """ With one department, both services build the same closed request from the same tasks """
    per_department = ClinicManager(DepartmentPatientRequestService())
    per_department.process_tasks_update(inputs[0])
    per_department.process_tasks_update(inputs[1])
    department_closed = request_for('patient1', 'Primary', 'Closed')

    db.clinic.drop_tables()
    for table in (db.patient_requests, db.tasks, db.task_events):
        table.clear_cache()
    per_patient = ClinicManager(PerPatientRequestService())
    per_patient.process_tasks_update(inputs[0])
    per_patient.process_tasks_update(inputs[1])
    (patient_closed,) = requests_of('patient1', 'Closed')

    for field in ('status', 'assigned_to', 'messages', 'medications', 'task_ids',
                  'created_date', 'updated_date', 'pharmacy_id'):
        assert department_closed[field] == patient_closed[field], field


def test_data_of_a_task_closed_while_the_request_is_open_is_dropped_from_it(manager):
    """ README: the data of a closed task is removed from the request that is still open """
    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task1', 'patient1', 'Primary', message='first'),
        task_for('task2', 'patient1', 'Primary', message='second')]))

    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task1', 'patient1', 'Primary', status='Closed', message='first')]))

    (still_open,) = requests_of('patient1', 'Open')
    assert set(still_open['task_ids']) == {'task2'}
    assert still_open['messages'] == ['second']

    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task2', 'patient1', 'Primary', status='Closed', message='second')]))

    # task1 closed in an earlier update, its data is not in the history of the request
    (closed,) = requests_of('patient1', 'Closed')
    assert closed['id'] == still_open['id']
    assert set(closed['task_ids']) == {'task2'}
    assert closed['messages'] == ['second']


def test_request_with_a_task_closed_and_another_moved_away_keeps_only_the_closed_data(manager):
    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task1', 'patient1', 'Primary', message='closes'),
        task_for('task2', 'patient1', 'Primary', message='moves')]))
    primary_id = request_for('patient1', 'Primary', 'Open')['id']

    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task1', 'patient1', 'Primary', status='Closed', message='closes'),
        task_for('task2', 'patient1', 'Radiology', message='moves')]))

    closed = request_for('patient1', 'Primary', 'Closed')
    assert closed['id'] == primary_id
    assert set(closed['task_ids']) == {'task1'}
    assert closed['messages'] == ['closes']
    assert request_for('patient1', 'Radiology', 'Open')['messages'] == ['moves']


def test_task_closed_and_moved_in_the_same_update(manager, inputs):
    """ task5 is closed in input 3 while also moving from Dermatology to Primary """
    for update in inputs[:2]:
        manager.process_tasks_update(update)
    dermatology_id = request_for('patient2', 'Dermatology', 'Open')['id']

    manager.process_tasks_update(inputs[2])

    assert requests_of('patient2', 'Open') == []
    # task5 closes in Primary, so the Dermatology request it left is empty and cleared
    dermatology = request_for('patient2', 'Dermatology', 'Closed')
    assert not dermatology['task_ids']
    assert dermatology['messages'] == [] and dermatology['medications'] == []
    primary = request_for('patient2', 'Primary', 'Closed')
    assert set(primary['task_ids']) == {'task400', 'task5'}
    assert len(primary['messages']) == 2
    (closed,) = events_of(task_id='task5', reason='closed')
    assert closed['from_request_id'] == dermatology_id
    assert closed['from_department'] == 'Dermatology'
    assert closed['to_request_id'] is None


def test_new_task_never_reopens_a_closed_request(manager, inputs):
    for update in inputs[:3]:
        manager.process_tasks_update(update)
    closed_ids = {r['id'] for r in requests_of('patient3', 'Closed')}
    assert closed_ids

    manager.process_tasks_update(inputs[3])

    (open_request,) = requests_of('patient3', 'Open')
    assert open_request['id'] not in closed_ids
    assert set(open_request['task_ids']) == {'task7'}
    assert all(r['status'] == 'Closed'
               for r in requests_of('patient3') if r['id'] in closed_ids)


def test_task_moving_back_to_a_closed_department_creates_a_new_request(manager, inputs):
    manager.process_tasks_update(inputs[0])
    original_radiology_id = request_for('patient3', 'Radiology', 'Open')['id']
    manager.process_tasks_update(inputs[1])

    move_back = task_for('task3', 'patient3', 'Radiology')
    manager.process_tasks_update(TaskInput(tasks=[move_back]))

    reopened = request_for('patient3', 'Radiology', 'Open')
    assert reopened['id'] != original_radiology_id
    assert request_for('patient3', 'Radiology', 'Closed')[
        'id'] == original_radiology_id
    assert set(request_for('patient3', 'Primary', 'Open')
               ['task_ids']) == {'task6'}


def test_replaying_an_update_changes_nothing(manager, inputs):
    manager.process_tasks_update(inputs[0])
    requests_before = db.patient_requests.all()
    events_before = db.task_events.all()

    manager.process_tasks_update(inputs[0])

    assert len(db.patient_requests.all()) == len(requests_before)
    assert len(db.task_events.all()) == len(events_before)
    assert {r['id'] for r in db.patient_requests.all()
            } == {r['id'] for r in requests_before}


def test_retrying_every_update_changes_nothing(manager, inputs):
    """ A retry right after an update, including the updates that close tasks, whose retry used to write
    the `closed` events again """
    for update in inputs:
        manager.process_tasks_update(update)
        requests_before = db.patient_requests.all()
        events_before = db.task_events.all()
        tasks_before = db.tasks.all()

        manager.process_tasks_update(update)

        assert db.patient_requests.all() == requests_before
        assert db.task_events.all() == events_before
        assert db.tasks.all() == tasks_before


def test_replaying_a_close_without_a_request_adds_no_event(manager):
    closed = TaskInput(tasks=[
        task_for('task9', 'patient9', 'Primary', status='Closed')])

    manager.process_tasks_update(closed)
    manager.process_tasks_update(closed)

    assert len(events_of(task_id='task9')) == 1


def test_a_task_closed_again_after_being_reopened_is_recorded(manager):
    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task9', 'patient9', 'Primary', status='Closed')]))
    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task9', 'patient9', 'Primary')]))
    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task9', 'patient9', 'Primary', status='Closed')]))

    assert [e['reason'] for e in events_of(task_id='task9')] == [
        'closed', 'created', 'closed']


def test_only_the_touched_patients_are_loaded(inputs):
    requested = []

    class SpyTaskService(TaskService):
        def get_open_tasks_for_patients(self, patient_ids):
            patient_ids = list(patient_ids)
            requested.append(set(patient_ids))
            return super().get_open_tasks_for_patients(patient_ids)

    manager = ClinicManager(DepartmentPatientRequestService(
        task_service=SpyTaskService()))

    manager.process_tasks_update(inputs[0])
    manager.process_tasks_update(inputs[3])

    assert requested == [{'patient1', 'patient2', 'patient3'}, {'patient3'}]


def test_task_closed_without_ever_having_a_request_is_recorded(manager):
    manager.process_tasks_update(TaskInput(tasks=[
        task_for('task9', 'patient9', 'Primary', status='Closed')]))

    assert requests_of('patient9') == []
    (closed,) = events_of(task_id='task9')
    assert closed['reason'] == 'closed'
    assert closed['from_request_id'] is None
    assert closed['to_request_id'] is None


def test_build_task_events_ignores_unchanged_tasks():
    primary = request_with('request-1', 'Primary', {'task1'})

    assert build_task_events(
        [task_for('task1', 'patient1', 'Primary')], [primary], [primary]) == []


def test_build_task_events_reasons():
    primary_before = request_with('request-1', 'Primary', {'closed'})
    radiology_before = request_with('request-2', 'Radiology', {'moved'})
    primary_after = request_with('request-1', 'Primary', {'moved', 'created'})
    changed = [task_for(task_id, 'patient1', 'Primary', status)
               for task_id, status in [('closed', 'Closed'), ('created', 'Open'),
                                       ('moved', 'Open'), ('unplaced', 'Closed')]]

    events = build_task_events(
        changed, [primary_before, radiology_before], [primary_after])

    assert [(e.task_id, e.reason, e.from_request_id, e.to_request_id) for e in events] == [
        ('closed', 'closed', 'request-1', None),
        ('created', 'created', None, 'request-1'),
        ('moved', 'moved', 'request-2', 'request-1'),
        ('unplaced', 'closed', None, None)]


def request_with(request_id, department, task_ids) -> PatientRequest:
    return PatientRequest(
        id=request_id, patient_id='patient1', status='Open', assigned_to=department,
        created_date=datetime(2023, 5, 3, tzinfo=timezone.utc),
        updated_date=datetime(2023, 5, 3, tzinfo=timezone.utc),
        messages=[], pharmacy_id=None, task_ids=task_ids)


def task_for(task_id, patient_id, department, status='Open', message='Moved back') -> PatientTask:
    return PatientTask(
        id=task_id, patient_id=patient_id, status=status, assigned_to=department,
        created_date=datetime(2023, 5, 3, tzinfo=timezone.utc),
        updated_date=datetime(2023, 7, 1, tzinfo=timezone.utc),
        message=message)


def test_update_logs_stats_summary(manager, inputs, caplog):
    caplog.set_level(logging.INFO)

    manager.process_tasks_update(inputs[0])

    (summary,) = [r.getMessage() for r in caplog.records
                  if r.getMessage().startswith('requests updated')]
    assert 'changed_tasks=' in summary and 'created=' in summary and 'duration_ms=' in summary


def test_update_logs_each_group_decision_at_debug(manager, inputs, caplog):
    caplog.set_level(logging.DEBUG)
    manager.process_tasks_update(inputs[0])
    manager.process_tasks_update(inputs[1])

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith('created request') for m in messages)
    assert any(m.startswith('closed request') for m in messages)
    assert any(' moved:' in m for m in messages)


def test_logs_contain_ids_and_counts_only(manager, inputs, caplog):
    caplog.set_level(logging.DEBUG)
    for update in inputs:
        manager.process_tasks_update(update)
    log_text = caplog.text
    assert log_text
    for task in (t for update in inputs for t in update.tasks):
        assert task.message not in log_text
        for medication in task.medications:
            assert medication.name not in log_text
    for department in ('Dermatology', 'Radiology', 'Primary'):
        assert department not in log_text


def test_failed_update_is_logged_without_the_error_message_and_reraised(inputs, caplog):
    class FailingService(DepartmentPatientRequestService):
        def update_requests(self, changed_tasks):
            raise RuntimeError('secret message of patient')

    manager = ClinicManager(FailingService())

    with pytest.raises(RuntimeError):
        manager.process_tasks_update(inputs[0])

    (error,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert 'updating requests' in error.getMessage()
    assert 'RuntimeError' in error.getMessage()
    assert 'secret' not in caplog.text


def test_failed_event_write_logs_task_ids_and_reraises(inputs, caplog):
    class FailingEventService:
        def record(self, events):
            raise OSError('disk full')

    manager = ClinicManager(DepartmentPatientRequestService(
        event_service=FailingEventService()))

    with pytest.raises(OSError):
        manager.process_tasks_update(inputs[0])

    assert 'failed to record' in caplog.text
    assert 'task1' in caplog.text


def test_two_open_requests_for_one_department_raise(manager, inputs):
    manager.process_tasks_update(inputs[0])
    duplicate = request_for('patient1', 'Primary', 'Open')
    db.patient_requests.insert({**duplicate, 'id': 'duplicate-request'})

    with pytest.raises(DuplicateOpenRequestError):
        manager.process_tasks_update(inputs[1])
