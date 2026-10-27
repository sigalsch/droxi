# CHANGES

## 1. Summary

Requests are now grouped by `(patient_id, department)` instead of by `patient_id` only. The department is the task's
`assigned_to`, and a task can move between departments.

The approach is to **reconcile the requests from the tasks, only for the patients touched by an update**, and to record
each task's path in an **append-only event log**. The code is in `DepartmentPatientRequestService`. The old
`PerPatientRequestService` keeps its behavior.

- `services/patient_department_request_service.py`: the new service.
- `services/task_event_service.py`, `models/task_event.py`, `db/db_tinydb.py`: the task events and their table.
- `services/abstract_patient_request_service.py`, `services/task_service.py`, `services/patient_request_service.py`,
  `clinic_manager.py`: `update_requests` now takes only the changed tasks, and `TaskService` can load the open tasks of
  given patients.
- `observability.py`, `main.py`: one INFO stats line per update. Log lines carry ids and counts only, never messages,
  medications or departments.
- Tests: `tests/test_department_requests_and_events.py` is new. The tests now run on TinyDB `MemoryStorage`
  (`tests/conftest.py`), and `__pycache__` and `tinydb/db.json` are no longer tracked.

Run `pytest` from the project root.

---

## 2. Design

### 2.1 The problem

The old service rebuilds a request from the tasks it receives. If grouping is simply changed to
`(patient_id, department)`, a move breaks it: a patient's only Dermatology task is reassigned to Primary, the next
update has no Dermatology task, so the Dermatology request is never revisited and stays `Open` with stale messages and
medications. Something must close a request whose last task left its department (patient3 goes from 2 open requests to
1 in update 2 of the sample data).

### 2.2 Decision 1: how to handle a move

**A. Incremental update with tracked membership.** Remember which request holds each task, detach it from the old
request on a move, close that request if it is empty, and attach the task to the new one.
- Cheapest per update, but the membership is a second source of truth that has to stay in sync with the requests, and it
  needs a multi-write transaction in production.

**B. Reconcile from the tasks (chosen).** Regroup the tasks by `(patient_id, department)` and bring the open requests in
line with the groups: rebuild a request whose group has open tasks, create one for a new group, close one whose group
has no open tasks.
- Nothing to keep in sync, requests are always derived from tasks, and one rule covers a move and a fully emptied
  department. The cost is that a request is rebuilt even if little changed, which is cheap at this scale.

### 2.3 Decision 2: scope of the reconcile

**A. Global.** Keep the old contract: `ClinicManager` passes all open tasks of all patients plus the newly closed ones.
- Smallest change, but every update (every 2 minutes) reads all open tasks and rewrites every open request for
  thousands of patients, to process dozens of changes.

**B. Only the touched patients (chosen).** `ClinicManager` passes only the changed tasks. The service loads the open
tasks and open requests of those patients and reconciles them.
- Work is proportional to the update size, which fixes the performance issue flagged in `ClinicManager`. The service
  interface changes (`update_requests(changed_tasks)`), and it needs a lookup of open tasks by patient.

### 2.4 How an update is processed

1. `ClinicManager` stores the changed tasks, then calls `update_requests(changed_tasks)`.
2. The service loads the open tasks and open requests of the touched patients only, and adds the tasks closed by this
   update (they are no longer open in the DB).
3. Tasks are grouped by `(patient_id, department)` and compared with the open requests:
   - group has open tasks, request exists: the request is rebuilt (existing `to_patient_request`), keeping its id,
   - group has open tasks, no request: a new request is inserted,
   - no open tasks, request exists: the request is closed (section 3.1),
   - no open tasks, no request: nothing happens.
4. The open requests before and after are compared, and one event is written per task that changed request.

A department that lost all its tasks has an open request but no open task, so the same rule that closes any request
closes it. It also covers `task5` in the sample data, which is closed and moved from Dermatology to Primary in one update.

### 2.5 Task events

Requests are rebuilt on every update, so they only hold the current state, and a closed request is a snapshot of its
last moment only. What happens in between leaves no trace in any request:
- **A partial close.** In update 2 of the sample data `task2` closes while patient2's Primary request stays open
  (because of `task400`). The rebuild drops `task2`'s data, as the README requires, and nothing in any request shows it
  was ever there.
- **A move out of a request that stays open.** Not in the sample data, but if one of two Radiology tasks moved to
  Primary, the Radiology request would be rebuilt without it and nothing would show that it left.

So each task change is written to an append-only `TaskEvents` collection, and the events record the path of each task
between requests. They are written after the requests and never read by the request flow, so a bug in them cannot corrupt
a request.

- **Shape:** one event per changed task, with the reason `created`, `moved` or `closed`, and the request and department
  before and after. `build_task_events(changed_tasks, open_before, open_after)` compares which open request held each
  task before and after the update, and a task that stays in the same request produces nothing. A task that arrives
  already closed and was never in an open request gets a `closed` event with no requests.
- **Replay:** retrying an update is safe. Tasks are upserted by id, a request is rebuilt with its existing id, and a
  replay produces no `created` or `moved` events. A replayed `closed` event looks new (no open request before or after),
  so `TaskEventService.record` skips a `closed` event without a `from_request_id` when the task's last event is already
  `closed`. A reopened task gets a `created` event, so a later close is recorded again.
- **Cost:** one event per created, moved or closed task, in one batch per update. The events hold patient identifiers,
  so they need retention and access rules.

---

## 3. Behavior choices

### 3.1 What a closed request keeps

Two README rules apply, and they do not conflict. "When a single task ... is `Closed`, the data of the task needs to be
removed" is about a request that is **still open**, and `to_patient_request` already rebuilds it from the open tasks
only. "Requests that were open and then closed are retained as medical history" is why `to_patient_request` keeps the
data of the closed tasks when the whole request closes. So a request closes in one of two ways:

| Why it closes | What the closed request holds |
|---|---|
| Its group has no open task and some tasks closed in this update | Built by `to_patient_request`, as `PerPatientRequestService` does, with the existing id. It keeps the messages, medications and ids of the tasks closed in it. |
| Its tasks all moved away | Cleared: no messages, no medications, no `task_ids`. |

**Why clear the emptied request.** Its last snapshot holds the content of tasks that are now open in another
department's request. Keeping it would show a second, out of sync copy in a closed request of the wrong department, as if
a doctor of that department had handled it. Where the tasks went is in the events. If the reviewers prefer a snapshot,
the change is to return the request with only its status and `task_ids` changed.

**A closed request does not list every task it ever held.** The data of a task that closed earlier, while the request
was open, was already dropped by the rebuild, so it holds the tasks closed in its **last** update, as in
`PerPatientRequestService`. Consequently `task_ids` can be empty: patient3's Radiology (all tasks moved away) is empty,
and `task5`, closed while moving, is listed in the request of its **new** department (Primary holds `task400` and
`task5`, Dermatology is empty). Nothing in the code assumes `task_ids` is non-empty.

### 3.2 Smaller choices

- **Closed requests are never reopened.** Only open requests are loaded, so a new task for a department whose request
  closed, including a task moving back, creates a **new** open request.
- **The interface changed.** `update_requests` takes the changed tasks only, and the base class holds the `TaskService`,
  so each service loads what it needs. `PerPatientRequestService` absorbed the old full scan, so its behavior and tests
  are intact.
- **Department key:** `assigned_to` is used as is. `PatientRequest` has no new field.
- **Closed request dates:** a request closed with tasks takes `created_date` and `updated_date` from the tasks closed in
  it. A request emptied by a move keeps its old dates.
- **`closed` events have no `to_department`:** a closed task is in no open request.
- **One open request per patient and department** is enforced in code: finding two raises `DuplicateOpenRequestError`.
- **Error handling:** failures are logged with ids, counts and the exception type only, never its message, and are
  re-raised so the caller can retry. There is no transaction, so if only the event write fails, a retry finds no change
  and does not write the missing events, and the task ids are logged for a backfill.

---

## 4. Answers to the questions

- **`TaskService.updates_tasks`:** TinyDB has no bulk upsert, so each task is a separate lookup and write. Without an
  index on the task id each lookup also scans the whole tasks table (about N x table size). There is no transaction, so
  a failure half way leaves the batch partly applied. MongoDB offers `bulkWrite` with upsert, a unique index on the task
  id for atomic upserts, and multi-document transactions.
- **`ClinicManager`:** it loaded every open task of every patient on each call, although an update touches dozens of
  tasks and only thousands of patients have open requests. The cost grew with the whole backlog instead of the update
  size. The department service now loads only the touched patients' tasks.
- **`PerPatientRequestService`:** same full scan (kept, to preserve the old behavior). It also runs one query per
  patient to find the open request (N+1) and rewrites every open request even when nothing changed.

**Other observations.** `get_open_tasks` returned a generator that was immediately turned into a list. The type of
`ClinicManager.__init__`'s argument was `PatientRequestService = None` and should be optional (fixed). The DB path
`tinydb/db.json` is relative to the working directory.

---

## 5. With a real DB, in production

TinyDB and stdlib `logging` fit the exercise. None of this is implemented.

- **One transaction per update** covering tasks, requests and events, so a retry is fully safe and a failed event write
  leaves no gap.
- **A partial unique index** on `(patient_id, assigned_to)` where `status = 'Open'`, so a concurrent double create fails
  instead of producing a duplicate.
- **A bulk upsert** (`INSERT ... ON CONFLICT` or `bulkWrite`) on a unique index on the task id, and indexes on
  `patient_id` and `status` for tasks and requests, and on `(task_id, timestamp)` for events.
- **Stale or duplicate delivery:** write a task only if its `updated_date` is newer than the stored one. Concurrent
  updates need row locks, optimistic versioning, or a queue partitioned by `patient_id`.
- **Event log:** partition by time, with a retention policy and access controls, because it holds patient identifiers.
- **Observability:** export the stats line as metrics (counters, a duration histogram), add tracing per update and
  structured JSON logs, and alert on failed updates, `DuplicateOpenRequestError`, and an update duration above the
  2 minute interval, keeping the rule of ids and counts only in logs.
