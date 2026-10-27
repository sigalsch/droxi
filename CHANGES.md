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
| [4. Behavior decisions](#4-behavior-decisions) | What a closed request keeps, never reopening, the interface change. |
| [4.5 Idempotency](#45-idempotency) | Why a retried or replayed update is safe, the one gap that was fixed, and what is not covered. |
| [5. Code changes](#5-code-changes) | One line per changed file. |
| [6. Trade-offs and production notes](#6-trade-offs-and-production-notes) | What a real DB would change, and known limits. |
| [7. Tests](#7-tests) | The tests that were added. |
| [8. Questions and observations](#8-questions-and-observations) | Answers to the `# Question` comments, and other issues found. |
| [9. Future suggestions for production](#9-future-suggestions-for-production) | A more robust database, and observability tools. |

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

After a move or a close, a rebuilt request no longer shows what it used to contain (the data of a closed task is
dropped by the rebuild), and a request emptied by a move has its data cleared (section 4.1). So the question is whether
to keep a record outside the requests.

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

**Why C:** a request only keeps the data of the tasks closed in its last update (section 4.1), and one emptied by a move
keeps nothing. So the requests cannot say which request or department held a task over time, or why a request closed.
The events are the record of that. Section 3.5 has the rationale and the shape of the events.

### 3.4 How an update is processed

1. `ClinicManager` stores the changed tasks, then calls `update_requests(changed_tasks)`.
2. The service takes the touched patient ids and loads **only those patients'** open tasks and open requests. The tasks
   closed by this update are added, because they are no longer open in the DB.
3. Tasks are grouped by `(patient_id, department)` and compared with the open requests, one per group:
   - **Group has an open task, request exists:** the request is rebuilt from the tasks (existing `to_patient_request`), keeping its id.
   - **Group has an open task, no request:** a new request is inserted.
   - **No open task, request exists:** the request is closed (see section 4.1). If tasks closed in it, it is built
     by `to_patient_request` and keeps their data. If its tasks all moved away, its data is cleared.
   - **No open task, no request:** nothing happens.
4. The open requests before and after are compared, and one event is written per task that changed request.

Why this handles moves: a department that lost all its tasks has an open request but no open task, so it is closed
by the same rule that closes any other request. It also covers `task5` in the test data, which is closed and moved
from Dermatology to Primary in the same update.

### 3.5 Audit events

**Why keep them.** Requests are rebuilt on every update, so after a move or a close nothing in them shows what they
used to contain.
- *Medical history:* answers which request and department handled a task at each point in time, and tells a request
  that closed because its tasks moved away from one a doctor handled. A closed request only holds the tasks closed in
  its last update, and one emptied by a move holds nothing (see section 4.1), so the events are what links a task to
  the requests it passed through.
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
- **Mostly write only:** the request flow never reads it, except for one lookup that skips a replayed `closed` event
  (section 4.5). Requests are written first and events last.
- **Timestamps:** the event sets its own UTC processing time. A request's `updated_date` comes from task data.

**Cost.** One extra event per task that was created, moved or closed, written in one batch per update. The events contain patient identifiers, so
retention and access rules apply (see section 6).

---

## 4. Behavior decisions

### 4.1 What a closed request keeps
Two README rules apply, and they do not conflict:
- "When a single task from possibly many tasks that compose a request is `Closed`, the data of the task needs to be
  removed" is about a request that is **still open**. `to_patient_request` already does it: it rebuilds an open
  request from the open tasks only.
- "Requests that were open and then closed are retained as they are medical history", and `to_patient_request`
  deliberately keeps the data of the closed tasks when the whole request closes ("We only care about the closed
  tasks if all the tasks are closed and we are closing the request").

So the department service does not add a rule. A request closes in one of two ways:

| Why it closes | What the closed request holds |
|---|---|
| Its group has no open task and some tasks closed in this update | Built by `to_patient_request`, exactly as `PerPatientRequestService` does, with the existing id. It keeps the messages and medications of the tasks closed in it, and their ids. |
| Its tasks all moved away (nothing closed in it) | Cleared: no messages, no medications, no `task_ids`. |

**Why clear the emptied request.** The alternative is to leave its last snapshot. But the snapshot holds the content of
tasks that are now open in the request of another department. Keeping it would show their messages and medications in
a closed request of the wrong department, a second copy of the data that is no longer in sync with the task, and it
would be mistaken for something a doctor of that department handled. Where the tasks went is in the events
(`moved`, with the old and the new request), and the content lives with the task in its current request. This
only affects an empty group, so it does not lose the history of a request that a doctor handled. If the reviewers
prefer a snapshot, the change is to return the request with only its status and `task_ids` changed.

**What is not in a closed request.** The data of a task that closed in an earlier update, while the request was still
open, was already dropped by the rebuild (the first rule). So a closed request holds the tasks that closed in its
**last** update, not every task it ever held, the same as in `PerPatientRequestService`. A test covers this.

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

- **Closed request dates:** a request closed with tasks gets `created_date` and `updated_date` from the tasks closed in
  it, as `to_patient_request` sets them for any request (so `created_date` is that of the oldest closed task, not of
  the first task the request ever held, as in `PerPatientRequestService`). A request emptied by a move keeps its old
  dates, the move is in the events.

- **`closed` events have no `to_department`:** a closed task is in no open request. Where the task is now is in the tasks table.

- **A closed task that was never in an open request** (for example its first update is a close) creates no request,
  but it is recorded as a `closed` event without requests (section 3.5). Replaying such an update does not record it
  again (section 4.5).

### 4.5 Idempotency

An update can be delivered twice, for example when the caller retries after a timeout or after
`process_tasks_update` raised. Processing the same update again must leave the same state. It is idempotent for
all the writes:

| Write | Why a replay is safe |
|---|---|
| Tasks | `TaskService.updates_tasks` upserts by task id, so the same task overwrites itself. |
| Requests | The requests are derived from the tasks, not from the update. A replay regroups the same tasks, finds the open request of the group and rebuilds it with its **existing id** (`_save` upserts by id). A request that was closed has no open request, so a replay cannot match it and does not create a new one (section 4.2). |
| Events | One event is written per **change of open request**. After the first run the tasks already sit in their final requests, so a replay finds no change and writes nothing (`created`, `moved`). |

**The gap that was fixed: `closed` events.** A task that closed has no open request before the update and none after,
and that is exactly how a replay of its close looks. So `build_task_events` produced the `closed` event again, and
every retry of an update with closed tasks appended duplicates to the log. `TaskEventService.record` now skips a
`closed` event that has no `from_request_id` when the **last event of that task is already `closed`**:
- A close that left an open request has `from_request_id` set. It is a real change and is always written, and
  it cannot repeat, because the request is already closed.
- A task that is reopened gets a `created` event, so a later close is no longer "already closed" and is recorded.
- `record` returns the events it wrote, and the stats log line counts those, not the candidates.
- Cost: one query on `task_id` per update, only when it contains closed tasks that left no request. This is the one
  place the request flow reads the events. It only decides whether to skip an audit row, so a wrong answer cannot
  corrupt a request (requests are written first). In production, index `task_events` on `(task_id, timestamp)`.

**Not covered:**
- **Retry after a failed event write.** The requests are already written, so the retry finds no change and does not
  write the missing `created` and `moved` events. This is the existing gap from section 6. The task ids are logged to
  backfill them, and only a transaction removes it.
- **Out-of-order or stale replays.** Replaying an old update after newer ones overwrites newer task state, because the
  upsert has no `updated_date` check. The README guarantees ordered delivery, so only a retry of the latest update is
  considered. Without that guarantee, skip a task whose stored `updated_date` is newer.
- **Concurrent processing of the same update** is not handled, there is no lock or unique constraint. The unique
  partial index of section 6 would turn a double create into an error.
- **`PerPatientRequestService`** is unchanged. It is idempotent for open tasks (it updates the existing open request),
  and has no events.

---

## 5. Code changes

`PatientTask` and `PatientRequest` are unchanged.

| File | Change |
|---|---|
| `services/patient_department_request_service.py` | `DepartmentPatientRequestService`, implementing section 3.4. |
| `services/task_event_service.py` | New. `build_task_events` and `TaskEventService`, whose `record` skips replayed `closed` events (section 4.5). |
| `models/task_event.py` | New `TaskEvent` model, exported from `models`. |
| `db/db_tinydb.py` | New `TaskEvents` table. |
| `services/task_service.py` | New `get_open_tasks_for_patients(patient_ids)`, and the answer to the `# Question` comment. |
| `services/abstract_patient_request_service.py` | New `update_requests(changed_tasks)` signature, and the `TaskService` is held here (section 4.3). |
| `services/patient_request_service.py` | Adapted to the new signature, with the same behavior. It now loads all open tasks itself. |
| `clinic_manager.py` | Passes the changed tasks, no longer loads every open task. The previous code stays as a comment with the answer to the `# Question`. The default service gets the manager's `TaskService`. |
| `observability.py` | New. The PHI logging rule, `configure_logging` and `elapsed_ms`. |
| `main.py` | Calls `configure_logging()` (INFO by default). |
| `tests/test_department_requests_and_events.py` | New tests (section 7). |

---

## 6. Trade-offs and production notes

**Correctness**
- **No transaction:** TinyDB cannot write requests, tasks and events atomically. With a real DB, wrap them in one
  transaction. The event write is last so a failure cannot leave a wrong request.
- **Error handling:** failures are logged with ids, counts and the exception type only, never its message (it may hold
  task data), and then re-raised, so the caller sees the failure and can retry. Retrying `process_tasks_update` is
  safe, storing tasks, reconciling requests and recording events are idempotent (section 4.5). `ClinicManager` logs which stage failed (storing tasks
  or updating requests). If only the event write fails, the requests are already updated and a retry produces no
  events, so the task ids are logged for a backfill. A single transaction would remove this gap.
- **One open request per patient and department** is enforced in code: finding two raises `DuplicateOpenRequestError`. In a real DB use a
  partial unique index on `(patient_id, assigned_to)` where `status = 'Open'`.
- **Ordering:** as stated in the README, there are no out-of-order or dropped events.

**Performance and storage**
- **Indexes:** `patient_id` and `status` on tasks and requests, and `patient_id` on events. TinyDB has none, so the
  patient lookups are scans here.
- **Observability:** stdlib `logging`, configured only in `main.py` through `observability.py` (library code just gets
  a logger). **PHI rule, written at the top of `observability.py`:** every log line logs ids and counts only, never
  messages, medications, names or free-form content. Departments are not logged either, because with a patient id
  they say where the patient was sent. A test checks that no message, medication or department reaches the logs. One INFO line
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
  - a request emptied by a move is closed and cleared, with empty `task_ids`,
  - a request closed with its tasks keeps their data and ids, and equals the closed request of
    `PerPatientRequestService` for the same tasks,
  - the data of a task closed while its request is open is dropped from it, and a request with one task closed and
    another moved away keeps only the closed one's data,
  - a task closed and moved in one update, including the `task_ids` of both requests,
  - a task that was closed without ever having a request is recorded,
  - a new task does not reopen a closed request, and a task moving back creates a new one,
  - replaying an update changes nothing, and retrying each of the exercise updates right after it changes nothing
    either, including the ones that close tasks,
  - replaying a close of a task that never had a request adds no event, and a task closed again after being
    reopened is recorded,
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
- `ClinicManager.__init__` typed its argument as `PatientRequestService = None`, it should be optional (fixed).
- The DB path `tinydb/db.json` is still relative to the working directory.

---

## 9. Future suggestions for production

TinyDB and stdlib `logging` fit the exercise. They are the limits listed in section 6, and a production deployment
should replace them. None of this is implemented.

### 9.1 A more robust database

Use a real database, such as PostgreSQL or MongoDB (the choice does not change the design, the reconcile is the same).

| Need | Today (TinyDB) | Production |
|---|---|---|
| Atomic update | Tasks, requests and events are written separately, so a failure leaves a partial update, and a failed event write leaves a gap (sections 4.5 and 6). | One transaction per update covering tasks, requests and events. The retry is then fully safe. |
| One open request per patient and department | Checked in code, raising `DuplicateOpenRequestError`. | A partial unique index on `(patient_id, assigned_to)` where `status = 'Open'`. A concurrent double create fails instead of producing a duplicate. |
| Bulk task upsert | One lookup and write per task. | A single bulk upsert (`INSERT ... ON CONFLICT` or `bulkWrite`) on a unique index on the task id. |
| Patient lookups | Full scans of the tasks and requests tables. | Indexes on `patient_id` and `status` for tasks and requests, on `(task_id, timestamp)` and `patient_id` for events. |
| Stale or duplicate delivery | The upsert overwrites with whatever arrives last. | Write a task only if its `updated_date` is newer than the stored one (a conditional update), and keep a processed-batch id to drop a duplicate delivery. |
| Concurrent updates | Not handled. | Row locks or optimistic versioning on the patient's requests, or process one patient at a time (a queue partitioned by `patient_id`). |
| Event log | A table that grows without bound. | Partition by time and apply a retention policy, with access controls because it holds patient identifiers. |
| Environments | A relative file path (the tests already run on `MemoryStorage`). | Connection settings from the environment, tests on an in-memory or temporary database, and schema changes through migrations. |

### 9.2 Observability tools

Today there is one INFO stats line per update and DEBUG lines per decision, written to stdout, and they follow the PHI
rule of ids and counts only (section 6). In production:

- **Metrics:** export the numbers of the stats line to Prometheus or OpenTelemetry, instead of parsing logs:
  - counters for `changed_tasks`, requests `created`, `rebuilt` and `closed`, and events by reason (including
    skipped replayed `closed` events),
  - a histogram of the update duration, and of the storing and updating stages separately,
  - a counter of failures by stage and exception type.
- **Tracing:** one trace per `process_tasks_update`, with spans for storing tasks, loading the patients, reconciling
  and recording events. It shows which stage is slow, and an update id ties it to the logs.
- **Structured logs:** JSON lines with a correlation id per update, shipped to a central system (ELK, Loki or
  Datadog). The PHI rule stays, and a log filter that drops unexpected fields makes it hold even if a developer adds
  one by mistake. Keep the test that checks that no message, medication or department is logged.
- **Alerts:**
  - any update that fails, and the failed event write that needs a backfill (it logs the task ids),
  - `DuplicateOpenRequestError`, which means the invariant broke,
  - update duration above the 2 minute interval between updates, because they would start to queue,
  - a rise in `closed` events without a request, or in events per update, which points to bad input or a reconcile bug.
- **Dashboards and audit:** updates per minute, duration percentiles, open requests per department, and a way for
  support to look up the events of a `task_id` or `patient_id` (section 3.5) with access logged.
- **Health checks:** a readiness check that tests the database connection, and a "last update processed" age that
  alerts if updates stop arriving.
