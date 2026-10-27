# CHANGES

## 1. Summary

Requests are now grouped by `(patient_id, department)` instead of by `patient_id` only. The department is the task's
`assigned_to`, and a task can move between departments.

The chosen approach is a **patient-scoped reconcile plus an append-only audit event log** (the three decisions are in
section 3). Each update regroups only the touched patients' tasks by department and brings their open requests in line with the
groups. The change is in `DepartmentPatientRequestService`. The old `PerPatientRequestService` keeps its behavior.

| Section | What it covers |
|---|---|
| [2. Problem](#2-problem) | Constraints, and why grouping by department breaks when a task moves. |
| [3. Design](#3-design) | Three decisions with their options, pros and cons, then the chosen flow and the audit events. |
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

Three decisions were taken in order. The first picks the approach, the other two refine it:

| # | Decision | Chosen |
|---|---|---|
| 1 | [How to handle a move](#31-decision-1-how-to-handle-a-move) | Reconcile the requests from the tasks |
| 2 | [How much to reconcile per update](#32-decision-2-scope-of-the-reconcile) | Only the patients touched by the update |
| 3 | [How to keep the move history](#33-decision-3-move-history) | An append-only event collection |

The sections after them describe the result: [3.4 how an update is processed](#34-how-an-update-is-processed) and
[3.5 the audit events](#35-audit-events).

### 3.1 Decision 1: how to handle a move

**A. Incremental update with tracked membership.** Remember which request holds each task (for example in the latest
audit event, or a `request_id` stored with the task). For each changed task, find the old request, detach the task,
recompute that request and close it if no open tasks remain, then find or create the new `(patient, department)`
request and attach the task.
- Pros: cheapest per update, only two requests are touched per task.
- Cons: the membership is a second source of truth that must stay in sync with the requests, and correctness depends on
  it. It needs an index on the task id and a multi-write transaction in production. More than "dozens of tasks per
  update" needs.

**B. Reconcile from the tasks (chosen).** Regroup the tasks by `(patient_id, department)` and bring the open requests in
line with the groups: rebuild a request whose group has open tasks, create one for a new group, close one whose group
has no open tasks.
- Pros: nothing to keep in sync, requests are always derived from tasks, as they were before. One rule covers a move and
  a fully emptied department.
- Cons: a request is rebuilt even if little changed.

**Why B:** the rebuild is cheap at this scale, and it fixes the stale-request problem (section 2.2) with the same rule
that closes any request, instead of tracking where each task lives. Decision 2 limits the rebuild to the touched patients.

### 3.2 Decision 2: scope of the reconcile

**A. Global.** Keep the old contract: `ClinicManager` passes all open tasks of all patients plus the newly closed ones.
Group them, and close any open request whose `(patient, department)` has no group.
- Pros: smallest change. No interface change, and the old service is untouched.
- Cons: every update, which arrives every 2 minutes, reads all open tasks and rewrites every open request for
  thousands of patients, to process dozens of changes. The service cannot tell which patients were touched, because the
  list mixes changed and unchanged tasks.

**B. Only the touched patients (chosen).** `ClinicManager` passes only the changed tasks. The service loads the open
tasks and open requests of those patients and reconciles them.
- Pros: work is proportional to the update size. It fixes the performance issue flagged in `ClinicManager`.
- Cons: the service interface changes (section 4.3), and it needs a lookup of open tasks by patient (an index on
  `patient_id` in a real DB).

**Why B:** the README says updates carry dozens of tasks, and only thousands of the millions of patients have open
requests, so a full scan on every call does not fit.

### 3.3 Decision 3: move history

After a move or a close, a rebuilt request no longer shows what it used to contain, and closed requests have their data
cleared (section 4.1). So the question is whether to keep a record outside the requests.

**A. No history.** Nothing is stored.
- Pros: simplest.
- Cons: a request that closed because its tasks moved away looks the same as one a doctor handled, and nothing says
  where a task went.

**B. Log lines.** Moves are written as structured log lines.
- Pros: no new collection.
- Cons: not durable or queryable, it lasts as long as the logs are kept.

**C. Event collection (chosen).** One document per task change, in an append-only `TaskEvents` collection that is only
written, never read by the request flow.
- Pros: durable and queryable. A bug in it cannot corrupt a request.
- Cons: an extra write per task change, a new collection to retain, and it holds patient identifiers.

**Why C:** with data cleared on close, the events are the only record of what happened to a request's tasks.
Section 3.5 has the rationale and the shape of the events.

### 3.4 How an update is processed

1. `ClinicManager` stores the changed tasks, then calls `update_requests(changed_tasks)`.
2. The service takes the touched patient ids and loads **only those patients'** open tasks and open requests. The tasks
   closed by this update are added, because they are no longer open in the DB.
3. Tasks are grouped by `(patient_id, department)` and compared with the open requests, one per group:
   - **Group has an open task, request exists:** the request is rebuilt from the tasks (existing `to_patient_request`), keeping its id.
   - **Group has an open task, no request:** a new request is inserted.
   - **No open task, request exists:** the request is closed and its data is cleared (see section 4.1).
   - **No open task, no request:** nothing happens.
4. The open requests before and after are compared, and one event is written per task that changed request.

Why this handles moves: a department that lost all its tasks has an open request but no open task, so it is closed
by the same rule that closes any other request. It also covers `task5` in the test data, which is closed and moved
from Dermatology to Primary in the same update.

### 3.5 Audit events

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

**Cost.** One extra event per task that was created, moved or closed, written in one batch per update. The events contain patient identifiers, so
retention and access rules apply (see section 6).

---

## 4. Behavior decisions

### 4.1 Clear the data on every request that closes
The README says that when a task is closed, its data must be removed. So any request that closes (all its tasks
closed, or all its tasks moved away) gets `messages=[]` and `medications=[]`. It stays as medical history, with its
`task_ids` and `updated_date` set as described in section 4.4.

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

### 4.4 Smaller choices and assumptions
- **Department key:** `assigned_to` is used as is. `PatientRequest` has no new field.
- **Assumption, closed request `task_ids` can be empty.** When a request closes, its `task_ids` are the tasks of its
  department that arrived closed in that update. Nothing else is available, because the other tasks are gone from the
  group. So:
  - closed because its tasks were closed: `task_ids` holds those tasks (patient1 Primary: `task1`, `task4`),
  - closed because all its tasks moved away: `task_ids` is **empty** (patient3 Radiology),
  - a task that closes while moving is listed in the request of its **new** department, and the request it left is
    empty (`task5`: Dermatology is empty, Primary holds `task400` and `task5`).

  A closed request therefore does not list every task it ever held. A task that closed in an earlier update, such as
  `task2`, is no longer in it. Where a task went is in the events (`from_request_id`). Nothing in the code assumes
  `task_ids` is non-empty. Both cases are covered by tests.
- **Closed request `updated_date`:** the newest of its own date and the dates of the tasks that closed in it, so it
  never goes back. A request emptied by a move keeps its old date, the move is in the events.
- **`closed` events have no `to_department`:** a closed task is in no open request. Where the task is now is in the tasks table.
- **A closed task that was never in an open request** (for example its first update is a close) creates no request,
  but it is recorded as a `closed` event without requests (section 3.5). Replaying such an update records it again.

---

## 5. Code changes

`PatientTask` and `PatientRequest` are unchanged.

| File | Change |
|---|---|
| `services/patient_department_request_service.py` | `DepartmentPatientRequestService`, implementing section 3.4. |
| `services/task_event_service.py` | New. `build_task_events` and `TaskEventService`. |
| `models/task_event.py` | New `TaskEvent` model, exported from `models`. |
| `db/db_tinydb.py` | New `TaskEvents` table. |
| `services/task_service.py` | New `get_open_tasks_for_patients(patient_ids)`, and the answer to the `# Question` comment. |
| `services/abstract_patient_request_service.py` | New `update_requests(changed_tasks)` signature, and the `TaskService` is held here (section 4.3). |
| `services/patient_request_service.py` | Adapted to the new signature, with the same behavior. It now loads all open tasks itself. |
| `clinic_manager.py` | Passes the changed tasks, no longer loads every open task. The previous code stays as a comment with the answer to the `# Question`. The default service gets the manager's `TaskService`. |
| `main.py` | Configures logging (INFO by default). |
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
- **Observability:** stdlib `logging`, configured only in `main.py` (library code just gets a logger). One INFO line
  per update with the stats (`changed_tasks`, `patients`, `created`/`rebuilt`/`closed` requests, events by reason,
  `duration_ms`). DEBUG lines show each group decision, each event and the load sizes. In production, export the same
  numbers as metrics (counters and a duration histogram) instead of parsing logs.
- **Events grow without bound** and contain patient identifiers. They need a retention policy and access rules.

---

## 7. Tests

- `tests/test_clinic_manager_with_departments.py` (the exercise tests) and `tests/test_clinic_manager.py` (the old
  behavior) pass.
- `tests/test_department_requests_and_events.py` adds:
  - move events with the old and new request and department,
  - a request that loses all its tasks is closed and cleared, with empty `task_ids`,
  - closing a request clears its data and keeps the tasks that closed in it,
  - a task closed and moved in one update, including the `task_ids` of both requests,
  - a task that was closed without ever having a request is recorded,
  - a new task does not reopen a closed request, and a task moving back creates a new one,
  - replaying an update changes nothing,
  - only the touched patients are loaded,
  - unit tests for the event diff.

Run with `pytest` from the project root.

---

## 8. Questions and observations

### 8.1 Answers to the questions in the code
- **`TaskService.updates_tasks`:** TinyDB has no bulk upsert, so each task is a separate lookup and write. Without an
  index on the task id each lookup also scans the whole tasks table (about N x table size). There is
  no transaction, so a failure half way leaves the batch partly applied. MongoDB offers `bulkWrite` with upsert, a
  unique index on the task id for atomic upserts, and multi-document transactions.
- **`ClinicManager`:** it loaded every open task of every patient on each call, although an update touches dozens of
  tasks and there are thousands of patients with open requests. The cost grew with the whole backlog instead of the
  update size. The department service now loads only the touched patients' tasks.
- **`PerPatientRequestService`:** same full scan (kept, to preserve the old behavior). In addition it runs one query
  per patient to find the open request (N+1) and rewrites every open request even when nothing changed.

### 8.2 Other observations
- `get_open_tasks` returned a generator that was immediately turned into a list.
- Unused imports and a repeated `task_date_getter` exist in the services.
- `ClinicManager.__init__` typed its argument as `PatientRequestService = None`, it should be optional (fixed).
- The DB path `tinydb/db.json` is relative to the working directory, and the tests write to the checked-in file.
  Tests should use `MemoryStorage` or a temporary file.
- `__pycache__` directories and `tinydb/db.json` are committed, and should be in `.gitignore`.
