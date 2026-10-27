import json
from tinydb import Query, TinyDB
from datetime import datetime
from uuid import uuid4
from tinydb_serialization import SerializationMiddleware
from tinydb_serialization.serializers import DateTimeSerializer
from tinydb_serialization import Serializer
from tinydb.storages import JSONStorage, MemoryStorage


class SetSerializer(Serializer):
    OBJ_CLASS = set

    def encode(self, obj):
        return json.dumps(list(obj))

    def decode(self, s):
        return set(json.loads(s))

serialization = SerializationMiddleware(JSONStorage)

serialization.register_serializer(DateTimeSerializer(), 'TinyDate')
serialization.register_serializer(SetSerializer(), 'TinySet')

clinic = TinyDB("tinydb/db.json", create_dirs=True, storage=serialization)
# clinic = TinyDB(storage=MemoryStorage)

patient_requests = clinic.table('PatientRequest')
tasks = clinic.table('Tasks')
# either creates a new database file or accesses an existing one named `my_tiny_database`


def init_db():

    print('\n--------------------------------------------\nInitializing the PatientRequest table')

    existing_docs = (len(patient_requests))
    if existing_docs > 100:
        print(f'PatientRequest table already initialized with {
              existing_docs} requests')
        return

    # Add some closed requests to the patient_requests table
    generated_requests = [{
        'id': str(uuid4()),
        'patient_id': 'patient1',
        'status': 'Closed',
        'assigned_to': 'Primary',
        'created_date': datetime.now(),
        'updated_date': datetime.now(),
        'messages': [f'message{i}'],
        'medications': [{'code': '1234', 'name': 'Advil 200 mg'}],
    } for i in range(1, 101)]

    patient_requests.insert_multiple(generated_requests)

    print(f'Inserted {len(patient_requests)} patient requests')
