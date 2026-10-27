# CHANGES

Status: **design phase**. No implementation has been chosen or written yet.
This file will be extended with the final changes, rationale and trade-offs once an option is selected.

---

## 1. Problem

Requests are currently grouped by `patient_id` only. They must now be grouped by `(patient_id, department)`,
where the department is the task's `assigned_to` (`Dermatology`, `Radiology`, `Primary`).
A task can move between departments.

### 1.1 Constraints

- The input `PatientTask` cannot be extended. Any extra state must live on our side (tasks table, requests, or a new collection).
- `tests/test_clinic_manager_with_departments.py` must pass. `tests/test_clinic_manager.py` covers the existing behavior.
- Implementation goes in `services/patient_department_request_service.py` (`DepartmentPatientRequestService`).
- Closed requests are retained as medical history.

### 1.2 The core difficulty: stale requests

`PerPatientRequestService` rebuilds a request from the tasks it receives and overwrites the open request.
If grouping is simply changed to `(patient_id, department)`, a move breaks it:

- A patient's only Dermatology task is reassigned to Primary.
- The next update contains no Dermatology tasks, so the Dermatology request is never revisited.
- It stays `Open` with stale messages and medications.

Whatever option is chosen must close a request whose last open task left its department.
The tests check this (patient3 goes from 2 open requests to 1 in update 2).

---

## 2. Design options

Two groups: options that add an audit collection, and an option that does not.

### 2.1 Options that add a collection

Both use a new `task_events` collection, one document per change:

| Field | Meaning |
|---|---|
| `task_id`, `patient_id` | which task and patient |
| `from_request_id`, `to_request_id` | `None` on creation or removal |
| `from_department`, `to_department` | department before and after |
| `reason` | `created`, `moved`, `closed` or `request_closed_empty` |
| `timestamp` | when it happened |

#### Why add this collection

Requests are rebuilt from their current tasks on every update, so after a move or a close the request no longer shows
what it used to contain. The collection keeps that history as an append-only record.

**Value for medical history**

- **Traceability:** answers "which request, and which department, handled this task at each point in time".
  Without it, a task that moved from Dermatology to Radiology leaves no trace in the Dermatology request.
- **Explains closed requests:** a request that closed because its last open task moved away looks the same as one
  a doctor handled. The `reason` field (`closed` vs `request_closed_empty`) tells them apart.
- **Preserves removed data context:** the README removes a closed task's data from the request. The event still
  records that the task existed, which request it belonged to, and when it left.
- **Audit and accountability:** requests are retained as medical history, so being able to reconstruct who held
  a patient's task and when is reasonable for audit and compliance.
- **Append-only:** events are never overwritten, unlike requests, which are rebuilt on every update.

**Value for debugging**

- **Diagnose wrong grouping:** shows why a request holds a given set of tasks, and when a task entered or left it.
- **Reproduce incidents:** the ordered events for a `task_id` or `patient_id` replay what happened in each update.
- **Catch stale or duplicate requests:** an open request with no recent events, or a task with two entering events
  and no leaving event, points to a reconcile bug.
- **Cheap to query:** a lookup by `task_id` or `patient_id` replaces searching logs, which may be rotated or lost.
- **Durable:** unlike log lines, events survive log retention limits and can be queried with the data they describe.

**Cost to weigh**

- One extra write per task move or close, and a new collection to index and retain.
- Event volume is low. Updates carry dozens of tasks, and only moves, closes and creations produce events.
- Retention and access rules apply, because the events contain patient identifiers.

#### Option 1: Audit-driven incremental update

- **Idea:** the latest event for a task says which request holds it now.
- **Per changed task:**
  1. Look up its latest event to find the old request.
  2. If the task closed or changed department, detach it, recompute that request, and close it if no open tasks remain.
  3. Find or create the open `(patient, department)` request, attach the task, recompute it, write an event.
- **Collection use:** read and written.
- **Pros:** cheapest per update, no scan. The stale-request case is solved directly. Full move history.
- **Cons:** correctness depends on the event log. Needs an index on `task_id` and `timestamp` (TinyDB has none) and a multi-write transaction in production. Likely more than the "dozens of tasks per update" assumption needs.

#### Option 2: Patient-scoped reconcile plus audit events

- **Idea:** for each patient in the update, load their open tasks and open requests. Regroup the tasks by department, then:
  - update a request whose group still exists,
  - create a request for a new group,
  - close a request whose group is empty.
- **Events:** diff each request's old and new `task_ids` and write an event per task that entered or left.
- **Collection use:** write-only history. Requests are always rebuilt from tasks, so an audit bug cannot corrupt them.
- **Pros:** work is proportional to the update size. One rule covers moves and fully emptied departments. Durable move history.
- **Cons:** `ClinicManager` and the service interface change slightly. The service needs the touched patient ids, and `TaskService` needs a lookup by patient ids.

### 2.2 Option without a new collection

#### Option 3: Patient-scoped reconcile, logging only

- **Idea:** the same reconcile as Option 2.
- **Moves:** recorded only as structured log lines, for example `task_moved task_id=... from_request=... to_request=...`.
- **Collection use:** none.
- **Pros:** smallest data-model change. Still fixes the stale-request case and the full-scan problem.
- **Cons:** history is not durable or queryable. It lasts only as long as logs are retained.

### 2.3 Alternative considered

**Global rebuild plus reconcile.** Keep the current `ClinicManager` contract (all open tasks plus newly closed ones),
group by `(patient_id, department)`, and for each patient in the update close any open request whose key was not
produced. It is the smallest diff but keeps the performance issue noted in `ClinicManager`:
every update loads all open tasks to process dozens of changes. It could replace Option 3 if `ClinicManager`
must stay untouched.

---

## 3. Open decisions

- **Which option to implement.** Not decided yet.
- **What an emptied request keeps.** When a request closes because its last open task moved away:
  - *Clear its data.* This is closer to the README wording ("the data of the task needs to be removed"). With Options 1 and 2 the audit trail becomes the only record of what the request contained.
  - *Keep the last task's data.* This matches the existing "all tasks closed" behavior in `abstract_patient_request_service.py`.
- **Moving back.** If a task later returns to a department whose request is already closed, a new open request is created. A closed request is never reopened.
- **Department field.** Use `assigned_to` as the department key, or add an explicit `department` field on `PatientRequest`.

---

## 4. Notes on the existing code

- `ClinicManager` loads all open tasks on every update (`# Question` about the performance issue). Options 2 and 3 address this by loading only the affected patients' tasks.
- `TaskService.updates_tasks` upserts one task at a time because TinyDB has no bulk upsert. A fuller database would offer bulk or atomic upserts.
- No multikey index on `task_ids` or an index on `patient_id` and `status` in TinyDB. A real database such as MongoDB would provide both.
