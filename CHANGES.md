# CHANGES

## 1. Summary

Requests are now grouped by `(patient_id, department)` instead of by `patient_id` only. The department is the task's
`assigned_to`, and a task can move between departments.

The chosen approach is a **patient-scoped reconcile plus an append-only audit event log** (Option 2 in section 3.1).
Each update regroups only the touched patients' tasks by department and brings their open requests in line with the
groups. The change is in `DepartmentPatientRequestService`. The old `PerPatientRequestService` keeps its behavior.

| Section | What it covers |
|---|---|
| [2. Problem](#2-problem) | Constraints, and why grouping by department breaks when a task moves. |
| [3. Design](#3-design) | The options considered, the chosen flow, and the audit events. |
| [4. Behavior decisions](#4-behavior-decisions) | Clearing data on close, never reopening, the interface change. |
| [5. Code changes](#5-code-changes) | One line per changed file. |
| [6. Trade-offs and production notes](#6-trade-offs-and-production-notes) | What a real DB would change, and known limits. |
| [7. Tests](#7-tests) | The tests that were added. |
| [8. Questions and observations](#8-questions-and-observations) | Answers to the `# Question` comments, and other issues found. |

---

## 2. Problem

### 2.1 Constraints

- The input `PatientTask` cannot be extended. Any extra state lives on our side: the tasks table, the requests, or a new collection.
- `tests/test_clinic_manager_with_departments.py` must pass, and `tests/test_clinic_manager.py` covers the existing behavior.
- The implementation goes in `services/patient_department_request_service.py`.
- Closed requests are retained as medical history.
- An update carries dozens of tasks, there are millions of patients and only thousands have open requests.

### 2.2 The core difficulty: stale requests

The old service rebuilds a request from the tasks it receives and overwrites the open request.
If grouping is simply changed to `(patient_id, department)`, a move breaks it:

- A patient's only Dermatology task is reassigned to Primary.
- The next update contains no Dermatology tasks, so the Dermatology request is never revisited.
- It stays `Open` with stale messages and medications.

Whatever is built must close a request whose last open task left its department. The tests check this:
patient3 goes from 2 open requests to 1 in update 2.

---

## 3. Design

### 3.1 Options considered

Options 1 and 2 add a `task_events` collection with one document per change (task, patient, request and department
before and after, reason, timestamp). Option 3 does not.

**Option 1: audit-driven incremental update.** The latest event of a task says which request holds it. For each changed
task, find the old request, detach the task and recompute that request (close it if no open tasks remain), then find or
create the open `(patient, department)` request and attach the task.
- Pros: cheapest per update, no scan. Moves are solved directly.
- Cons: correctness depends on the event log. It needs an index on `task_id` and `timestamp`, and a multi-write
  transaction in production. More than "dozens of tasks per update" needs.

**Option 2 (chosen): patient-scoped reconcile plus audit events.** For each patient in the update, load their open tasks
and open requests, regroup the tasks by department, and update, create or close requests so they match the groups.
Events are derived afterwards and only written.
- Pros: work is proportional to the update size. One rule covers moves and fully emptied departments. The requests are
  always rebuilt from tasks, so a bug in the event log cannot corrupt them. Durable move history.
- Cons: the service interface changes slightly (see section 4.3), and a request is rebuilt even if little changed.

**Option 3: patient-scoped reconcile, logging only.** The same reconcile, with moves written as log lines.
- Pros: smallest data-model change.
- Cons: history is not durable or queryable, it lasts as long as the logs.

**Alternative: global rebuild plus reconcile.** Keep the old contract (all open tasks plus newly closed ones), group
them by `(patient_id, department)`, and close open requests of the touched patients whose key was not produced.
- Pros: smallest diff.
- Cons: keeps the full scan on every update.

### 3.2 How an update is processed (Option 2)

1. `ClinicManager` stores the changed tasks, then calls `update_requests(changed_tasks)`.
2. The service takes the touched patient ids and loads **only those patients'** open tasks and open requests.
3. Tasks are grouped by `(patient_id, department)` and compared with the open requests, one per group:
   - **Group has an open task, request exists:** the request is rebuilt from the tasks (existing `to_patient_request`), keeping its id.
   - **Group has an open task, no request:** a new request is inserted.
   - **No open task, request exists:** the request is closed and its data is cleared (see section 4.1).
   - **No open task, no request:** nothing happens.
4. The open requests before and after are compared, and one event is written per task that changed request.

Why this handles moves: a department that lost all its tasks has an open request but no open task, so it is closed
by the same rule that closes any other request. It also covers `task5` in the test data, which is closed and moved
from Dermatology to Primary in the same update.

### 3.3 Audit events

**Why keep them.** Requests are rebuilt on every update, so after a move or a close nothing in them shows what they
used to contain.
- *Medical history:* answers which request and department handled a task at each point in time, and tells a request
  that closed because its tasks moved away from one a doctor handled. Closed requests have their data cleared (see
  section 4.1), so the events are the only record of what happened to their tasks.
- *Audit:* reconstructing who held a patient's task and when is reasonable for compliance.
- *Debugging:* the ordered events of a `task_id` or `patient_id` replay what each update did, and a task with two
  entering events and no leaving event points to a reconcile bug. A query replaces searching rotated logs.

**Shape.**
- **Reasons:** `created`, `moved` and `closed`, always about one task. There is no request-level event: a request can
  only lose tasks by moving or closing them, so why it closed shows in its tasks' events.
- **One event per changed task:** `build_task_events(changed_tasks, open_before, open_after)` goes through the changed
  tasks and compares which open request held each one before and after the update. It takes plain `PatientRequest`
  lists, which already carry the ids and department, so no extra types are needed.
- **Tasks without a request are recorded too:** a task that arrives already closed and was never in an open request
  gets a `closed` event with no request on either side. No request is created for it.
- **Unchanged is not an event:** a task that stays in the same request produces nothing, so replaying an update of open
  tasks adds no events.
- **Write only:** nothing reads the log. Requests are written first and events last.
- **Timestamps:** the event sets its own UTC processing time. A request's `updated_date` comes from task data.

**Cost.** One extra write per task that moved, closed or was created. The events contain patient identifiers, so
retention and access rules apply (see section 6).

---

## 4. Behavior decisions

### 4.1 Clear the data on every request that closes
The README says that when a task is closed, its data must be removed. So any request that closes (all its tasks
closed, or all its tasks moved away) gets `messages=[]` and `medications=[]`. It keeps `task_ids` (only the tasks that
closed in it) and `updated_date`, so it remains as medical history.

This is a deliberate change from `PerPatientRequestService`, which keeps the data of the closed tasks on the closed
request. That service is unchanged, so its tests still describe the old behavior.

### 4.2 Closed requests are never reopened
Only open requests are loaded for the reconcile, so a closed request can never be matched or updated. A new task for a
department whose request was closed, including a task moving back, creates a **new** open request.

### 4.3 The service interface changed
`update_requests` now takes the changed tasks only, and the base class holds the `TaskService`, so each service loads
the other tasks it needs. With the old contract (all open tasks plus newly closed ones) a service cannot tell which
patients were touched, so the full scan stays. `PerPatientRequestService` absorbed that scan, so its behavior and tests
are intact.

### 4.4 Smaller choices
- **Department key:** `assigned_to` is used as is. `PatientRequest` has no new field.
- **Closed request `task_ids`:** only the tasks that closed in it. Tasks that moved away are in the events.
- **Closed request `updated_date`:** the newest date of the tasks that closed in it. A request emptied by a move
  keeps its old date, the move is in the events.
- **`closed` events have no `to_department`:** a closed task is in no open request. Where the task is now is in the tasks table.
- **A closed task that was never in an open request** (for example its first update is a close) creates no request,
  but it is recorded as a `closed` event without requests (section 3.3). Replaying such an update records it again.

---

## 5. Code changes

`PatientTask` and `PatientRequest` are unchanged.

| File | Change |
|---|---|
| `services/patient_department_request_service.py` | `DepartmentPatientRequestService`, implementing section 3.2. |
| `services/task_event_service.py` | New. `build_task_events` and `TaskEventService`. |
| `models/task_event.py` | New `TaskEvent` model, exported from `models`. |
| `db/db_tinydb.py` | New `TaskEvents` table. |
| `services/task_service.py` | New `get_open_tasks_for_patients(patient_ids)`. |
| `services/abstract_patient_request_service.py` | New `update_requests(changed_tasks)` signature, and the `TaskService` is held here (section 4.3). |
| `services/patient_request_service.py` | Adapted to the new signature, with the same behavior. |
| `clinic_manager.py` | Passes the changed tasks, no longer loads every open task. |
| `tests/test_department_requests_and_events.py` | New tests (section 7). |

---

## 6. Trade-offs and production notes

**Correctness**
- **No transaction:** TinyDB cannot write requests, tasks and events atomically. With a real DB, wrap them in one
  transaction. The event write is last so a failure cannot leave a wrong request.
- **One open request per patient and department** is enforced in code: finding two raises an error. In a real DB use a
  partial unique index on `(patient_id, assigned_to)` where `status = 'Open'`.
- **Ordering:** as stated in the README, there are no out-of-order or dropped events.

**Performance and storage**
- **Indexes:** `patient_id` and `status` on tasks and requests, and `patient_id` on events. TinyDB has none, so the
  patient lookups are scans here.
- **Events grow without bound** and contain patient identifiers. They need a retention policy and access rules.

---

## 7. Tests

- `tests/test_clinic_manager_with_departments.py` (the exercise tests) and `tests/test_clinic_manager.py` (the old
  behavior) pass.
- `tests/test_department_requests_and_events.py` adds:
  - move events with the old and new request and department,
  - a request that loses all its tasks is closed and cleared,
  - closing a request clears its data,
  - a task closed and moved in one update,
  - a task that was closed without ever having a request is recorded,
  - a new task does not reopen a closed request, and a task moving back creates a new one,
  - replaying an update changes nothing,
  - only the touched patients are loaded,
  - unit tests for the event diff.

Run with `pytest` from the project root.

---

## 8. Questions and observations

### 8.1 Answers to the questions in the code
- **`TaskService.updates_tasks`:** TinyDB has no bulk upsert, so each task is a separate lookup and write, and there is
  no transaction, so a failure half way leaves the batch partly applied. MongoDB offers `bulkWrite` with upsert, a
  unique index on the task id for atomic upserts, and multi-document transactions.
- **`ClinicManager`:** it loaded every open task of every patient on each call, although an update touches dozens of
  tasks and there are thousands of patients with open requests. The cost grew with the whole backlog instead of the
  update size. The department service now loads only the touched patients' tasks.

### 8.2 Other observations
- `get_open_tasks` returned a generator that was immediately turned into a list.
- Unused imports and a repeated `task_date_getter` exist in the services.
- `ClinicManager.__init__` typed its argument as `PatientRequestService = None`, it should be optional (fixed).
- The DB path `tinydb/db.json` is relative to the working directory, and the tests write to the checked-in file.
  Tests should use `MemoryStorage` or a temporary file.
- `__pycache__` directories and `tinydb/db.json` are committed, and should be in `.gitignore`.
