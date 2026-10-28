# Flow of a tasks update

Diagrams for [CHANGES.md](CHANGES.md) section 2.4. Run with `python main.py`, or call
`ClinicManager.process_tasks_update(task_input)`.

## 1. From update to requests and events

```mermaid
sequenceDiagram
    participant Caller as Caller (every ~2 min)
    participant CM as ClinicManager
    participant TS as TaskService
    participant RS as DepartmentPatientRequestService
    participant ES as TaskEventService
    participant DB as TinyDB

    Caller->>CM: process_tasks_update(task_input)
    CM->>TS: updates_tasks(tasks)
    TS->>DB: upsert each task by id
    CM->>RS: update_requests(changed_tasks)
    RS->>DB: load open requests of the touched patients
    RS->>TS: get_open_tasks_for_patients(patient_ids)
    TS->>DB: open tasks of the touched patients only
    Note over RS: add the tasks closed by this update,<br/>group by (patient_id, department)
    loop each group
        RS->>DB: upsert the rebuilt, new or closed request
    end
    Note over RS: build_task_events(changed_tasks,<br/>open_before, open_after)
    RS->>ES: record(events)
    ES->>DB: append to TaskEvents (skips replayed closed events)
    RS-->>CM: INFO "requests updated: ..."
```

## 2. Reconcile of one group `(patient_id, department)`

```mermaid
flowchart TD
    A["Group: the patient's open tasks in this department<br/>+ tasks closed by this update"] --> B{"Any open task<br/>in the group?"}
    B -- yes --> C{"Open request<br/>exists for the group?"}
    C -- yes --> D["REBUILT<br/>rebuild from the tasks, keep the request id"]
    C -- no --> E["CREATED<br/>insert a new open request"]
    B -- no --> F{"Open request<br/>exists for the group?"}
    F -- no --> G["NONE<br/>nothing happens"]
    F -- yes --> H{"Did tasks close<br/>in it in this update?"}
    H -- yes --> I["CLOSED, keeps history<br/>built from the closed tasks,<br/>keeps their data and ids"]
    H -- no --> J["CLOSED, cleared<br/>all its tasks moved away:<br/>no messages, medications or task_ids"]
```

A closed request is never reopened. A new task for that department creates a new open request.

## 3. How an event is decided

```mermaid
flowchart TD
    A["For each changed task"] --> B["Find the open request holding it<br/>before and after the update"]
    B --> C{"Same request<br/>before and after?"}
    C -- yes --> D["No event"]
    C -- no --> E{"Held after<br/>the update?"}
    E -- "no" --> F["closed"]
    E -- "yes, not before" --> G["created"]
    E -- "yes, another request before" --> H["moved"]
    F --> I{"No request before either,<br/>and last event of the task<br/>is already closed?"}
    I -- yes --> J["Skipped: a replayed update"]
    I -- no --> K["Appended to TaskEvents"]
    G --> K
    H --> K
```

## 4. Example: the sample data

| Update | What happens | Requests | Events |
|---|---|---|---|
| 1 | six tasks arrive | 5 created (patient1 Primary, patient2 Primary, patient2 Dermatology, patient3 Radiology, patient3 Primary) | 6 `created` |
| 2 | `task1`, `task2`, `task4` close, `task3` moves Radiology to Primary, `task400` arrives | 3 rebuilt (patient2 Primary without `task2`, patient2 Dermatology, patient3 Primary), 2 closed (patient1 Primary with its data, patient3 Radiology cleared) | 3 `closed`, 1 `moved`, 1 `created` |
| 3 | `task3`, `task5`, `task6`, `task400` close, `task5` moves Dermatology to Primary | 3 closed: patient3 Primary and patient2 Primary keep their data, patient2 Dermatology is cleared | 4 `closed` |
| 4 | `task7` arrives | 1 created | 1 `created` |
