import os

# Must be set before db.db_tinydb is imported by any test module, so the tests run on an
# in-memory TinyDB and never read or write tinydb/db.json.
os.environ['DROXI_DB_STORAGE'] = 'memory'
