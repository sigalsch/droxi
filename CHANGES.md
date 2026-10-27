# CHANGES

Requests are now grouped by `(patient_id, department)` instead of by `patient_id` only, where the department is the
task's `assigned_to`. A task can move between departments.

Implemented option: **patient-scoped reconcile plus an audit event log** (called Option 2 in the design discussion).

---

## 1. What changed

| File | Change |
|---|---|
| `services/patient_department_request_service.py` | New `DepartmentPatientRequestService` implementation (see section 2). |
| `services/task_event_service.py` | New. `build_task_events` is a pure function that derives audit events by comparing which open request held each task before and after an update. `TaskEventService` writes them. |
| `models/task_event.py` | New `TaskEvent` model, exported from `models`. |
| `db/db_tinydb.py` | New `TaskEvents` table, written only. |
| `services/task_service.py` | New `get_open_tasks_for_patients(patient_ids)`. |
| `services/abstract_patient_request_service.py` | `update_requests(changed_tasks)` now receives only the changed tasks. The base class holds the `TaskService`, so each service loads the other tasks it needs. |
| `services/patient_request_service.py` | Adapted to the new signature. Behavior is unchanged, it now loads all open tasks itself. |
| `clinic_manager.py` | Stops loading every open task on each call, and passes the changed tasks to the service. |
| `tests/test_department_requests_and_events.py` | New tests (see section 5). |

`PatientTask` is untouched, as required. `PatientRequest` is unchanged: `assigned_to` already identifies the department.

---

## 2. How an update is processed

1. `ClinicManager` stores the changed tasks, then calls `update_requests(changed_tasks)`.
2. The service takes the touched patient ids and loads **only those patients'** open tasks and open requests.
3. For each patient, tasks are grouped by department and compared with the open requests, one per department:
   - **Group has an open task, request exists:** the request is rebuilt from the tasks (existing `to_patient_request`), keeping its id.
   - **Group has an open task, no request:** a new request is inserted.
   - **No open task, request exists:** the request is closed and its data is cleared.
   - **No open task, no request:** nothing happens.
4. For each task, the open request that held it before is compared with the one that holds it after, and one event is written per difference.

Why this handles moves: a department that lost all its tasks has an open request but an empty group, so it is closed
by the same rule that closes any other request. This also covers `task5` in the test data, which is closed and moved
from Dermatology to Primary in the same update.

---

## 3. Choices and rationale

### Patient-scoped reconcile, not incremental patching
Requests are rebuilt from their tasks on every update, as they were before, so there is no second source of truth to
drift out of sync. Work is proportional to the touched patients, not to all open tasks, which fixes the
performance issue flagged in `ClinicManager`. Rebuilding a request is cheap, an update has dozens of tasks.

### Clear the data on every request that closes
The README says that when a task is closed, its data must be removed. So any request that closes
(all its tasks closed, or all its tasks moved away) gets `messages=[]` and `medications=[]`.
It keeps `task_ids` (only the tasks that closed in it) and `updated_date`, so it remains as medical history.
The event log is the record of what happened to its tasks. This is a deliberate change from the old behavior of
`PerPatientRequestService`, which kept the data of the closed tasks on the closed request. That service is
unchanged, so its tests still describe the old behavior.

### Closed requests are never reopened
Only open requests are loaded for the reconcile, so a closed request can never be matched or updated. A new task for a
department whose request was closed, including a task moving back, creates a **new** open request.
This is covered by tests.

### Audit events as an append-only log
- **Why:** requests are rebuilt on every update, so after a move or a close nothing in them shows what they used to
  contain. A request that closed because its tasks moved away would look the same as one a doctor handled.
- **Event reasons:** `created`, `moved`, `closed`, and a request-level `request_closed_empty` (with `task_id=None`).
- **Derived by diff:** events come from comparing old and new membership, so a task that did not change request
  produces nothing. Replaying an update adds no events.
- **Write only:** nothing reads the log, so a bug in it can never corrupt a request. Requests are written first, events last.
- **Timestamps:** events use processing time (UTC). A request's `updated_date` comes from task data, not the clock.

### The service interface changed
`update_requests` now takes the changed tasks only. With the old contract (all open tasks plus newly closed ones) the
service cannot tell which patients were touched, and the full scan stays. The legacy service absorbed that scan,
so the old behavior and tests are intact.

---

## 4. Trade-offs and assumptions

- **No transaction:** TinyDB cannot write requests, tasks and events atomically. With a real DB, wrap them in one
  transaction. The event write is last so a failure cannot leave a wrong request.
- **One open request per patient and department** is enforced in code: finding two raises an error. In a real DB use a
  partial unique index on `(patient_id, assigned_to)` where `status = 'Open'`.
- **Indexes:** `patient_id` and `status` on tasks and requests, and `patient_id` on events, otherwise the patient
  lookups are scans in a real DB. TinyDB has none, so they are scans here.
- **Events grow without bound** and contain patient identifiers. They need a retention policy and access rules.
- **Ordering:** as stated in the README, there are no out-of-order or dropped events.
- **A closed task that was never in an open request** (for example the first update ever for it is a close) creates no
  request and no event.
- **Closed request content:** a closed request keeps only the ids of the tasks that closed in it. Tasks that moved
  away are recorded in the events, not in the closed request.

### Options considered
- **Incremental update driven by the event log:** the latest event of a task says which request holds it, so only two
  requests are touched per task. Cheapest per update, but correctness would depend on the log, it needs an index on
  `task_id` and a multi-write transaction, and it is more than "dozens of tasks per update" needs.
- **Reconcile with logging only:** the same reconcile with moves logged as text. Smallest change, but history is lost
  with log retention and cannot be queried.
- **Global rebuild plus reconcile:** keep the old contract, group all open tasks, and close requests that were not
  produced. Smallest diff, but it keeps the full scan on every update.

---

## 5. Tests

- `tests/test_clinic_manager_with_departments.py` (the exercise tests) and `tests/test_clinic_manager.py` (the old
  behavior) pass.
- `tests/test_department_requests_and_events.py` adds:
  - move events with the old and new request and department,
  - a request that loses all its tasks is closed and cleared,
  - closing a request clears its data,
  - a task closed and moved in one update,
  - a new task does not reopen a closed request, and a task moving back creates a new one,
  - replaying an update changes nothing,
  - only the touched patients are loaded,
  - unit tests for the event diff.

Run with `pytest` from the project root.

---

## 6. Answers to the questions in the code

- **`TaskService.updates_tasks`:** TinyDB has no bulk upsert, so each task is a separate lookup and write, and there is
  no transaction, so a failure half way leaves the batch partly applied. MongoDB offers `bulkWrite` with upsert, a
  unique index on the task id for atomic upserts, and multi-document transactions.
- **`ClinicManager`:** it loaded every open task of every patient on each call, although an update touches dozens of
  tasks and there are thousands of patients with open requests. The cost grew with the whole backlog instead of the
  update size. The department service now loads only the touched patients' tasks.

## 7. Other observations

- `get_open_tasks` returned a generator that was immediately turned into a list.
- Unused imports and a repeated `task_date_getter` exist in the services.
- `ClinicManager.__init__` typed its argument as `PatientRequestService = None`, it should be optional (fixed).
- The DB path `tinydb/db.json` is relative to the working directory, and the tests write to the checked-in file.
  Tests should use `MemoryStorage` or a temporary file.
- `__pycache__` directories and `tinydb/db.json` are committed, and should be in `.gitignore`.
