from os import PathLike
from pathlib import Path
from models import TaskInput
import db.db_tinydb as db


cur_dir = Path(__file__).parent

files = ['./data/input_task_1.json',
         './data/input_task_2.json', './data/input_task_3.json', './data/input_task_4.json']


def load_input(file: PathLike):
    with open(file) as f:
        input_json = f.read()

    task_input = TaskInput.model_validate_json(input_json)
    return task_input


def load_all_inputs():
    return [load_input(cur_dir / f) for f in files]


def main():
    db.init_db()
    inputs = load_all_inputs()

    # Optional placeholder


if __name__ == '__main__':
    main()
