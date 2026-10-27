# Overview 


Before you is a part of a task management system for clinics. This part is responsible for ingestion of tasks from an external system. The input is a list of  [PatientTask](models/patient_task.py) objects. These tasks can represent questions from a patient to a doctor, requests for medications. The reply mechanism is out of scope. Each task can be modified with changes to the message or the medications fields. 


The purpose of this system is to accept tasks and intelligently merge them by patient id, creating a [PatientRequest](models/patient_request.py) object, often referred to as just request. This is to simplify the work load on medical practitioners. PatientRequest objects are queried and modified in a different system, but this is also out of scope. You do not need to worry about PatientRequests being modified or syncing back data to the external system.  


A task also has a status field. A task starts in the `Open` status, and it can be updated to the `Closed` status.
Since the tasks come from an external system, a doctor can choose at any time to handle the task in that external system. This is modeled by the `status` field on the `PatientTask`. The status is either `Open` or `Closed`. When a single task from possible many tasks that compose a request is `Closed`, the data of the task needs to be removed, but the task itself can remain. While a PatientRequest is a collection of tasks, as long as it is `Open`, it hasn't been handled by a doctor and can be modified. 


The top level API is ClinicManager, it has one method called process_tasks_update.
This method is called periodically, for example once every 2 minutes, and it accepts all the changes there were made to the patient tasks since the last call.

The code comes with a test set that can be found in  [tests/test_clinic_manager.py](tests/test_clinic_manager.py)

# Design assumptions

1. There are  millions of patients, but only thousands of them have open requests.
1. Each "tasks update" event is for dozens of tasks covering multiple users. 
1. Requests that were open and then closed are retained as they are medical history.
1. There might be thousands of doctors. 
1. There is no need to worry about out-of-order events or dropped events.  

# Exercise 

Currently, tasks are grouped only by the patient id. However, a new requirement emerged. In addition to the patient, the requests should also be grouped by the department specified in the assigned_to field. Possible values are ['Dermatology', 'Radiology', 'Primary']. 

It is possible for a task to be moved from one department to another. The exercise is to modify the existing code to support these. 

You are expected to implement the new solution in [services/DepartmentPatientRequestService](services/patient_department_request_service.py). 

There are two tests sets under the tests library. One for the initial implementation and another for the exercise solution in [tests/test_clinic_manager_with_departments.py](tests/test_clinic_manager_with_departments.py)

There are two test files in the tests directory:

* [tests/test_clinic_manager.py ](tests/test_clinic_manager.py) – This test file validates the current implementation, which only groups tasks by patient id.
* [tests/test_clinic_manager_with_departments.py](tests/test_clinic_manager_with_departments.py) – This test file validates the new requirement, which is to group tasks by both patient id and department.

<u>In summary:</u>

1. Modify the code to support grouping by both patient id and department.
1. Ensure the new implementation passes the tests in: tests/test_clinic_manager_with_departments.py.
1. In addition to passing tests, optimize the code for production-level readability and maintainability (see section [#Guidelines](#guidelines) below).
1. Answer the questions (see section [#Questions](#questions) below)
1. Add a CHANGES.md file summarizing:
    * The changes you made.
    * The rationale behind those changes.
    * Any trade-offs or assumptions you considered.

# Questions

In addition, there are several "# Question" comments in the code. Answer them. If you see issues with the code or have suggestions for improvements, please send them. 

# Guidelines

* You might need to modify the models or add additional tables/collections, this is perfectly fine, but the tests must pass. The input PatientTask can't be extended.

* Consider what happens when all the open tasks in one department are reassigned to another department. 

* While this is a toy example, we expect to see production level code in regards to readability. The code should be both correct and understandable. 

* If you encounter a dilemma in regards to performance characteristics or design, you can of course ask us, but you can also document it and explain the choice you made. As long as the tests pass, the code is functionally correct... or you got us, extra points :) .

# Technical notes

- For simplicity, the code is based on a pure python in-memory Document DB called [TinyDB](https://tinydb.readthedocs.io/en/latest/). It is pretty lacking in features, so if you are missing something that MongoDB or a good DB would have, you can just mention it should be used and you depend on this feature.

- The tests use pytest. 

- The code is set up to write the database to a tindydb/db.json file. To simulate a "real db" we generate a 100 closed PatientRequest objects. If you want to see the result of the code you wrote in the DB, it will be simpler to disable this generation. Can be done by changing the [config](tests/config.py)  
