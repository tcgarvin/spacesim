import datetime
import gzip
import ujson as json
from os import makedirs
from os.path import exists
from typing import Dict

BASE_LOG_DIR = "log"
FILE_ENDING = "json.gz"

class SimulationLogger:
    def __init__(self, simulation_id : str):
        self.id = simulation_id
        self.log_dir = f"{BASE_LOG_DIR}/{self.id}"

    def log_state(self, tick, state : Dict):
        makedirs(self.log_dir, exist_ok=True)

        with gzip.open(f"{self.log_dir}/{tick}.{FILE_ENDING}", "wt", encoding="utf-8") as logfile:
            json.dump(state, logfile, ensure_ascii=False)

def make_log_dir_name():
    now = datetime.date.today().isoformat()

    available_dir_name = ""
    count = 0
    while available_dir_name == "":
        count += 1
        candidate_dir_name = f"{now}-{count}"
        if not exists(f"{BASE_LOG_DIR}/{candidate_dir_name}"):
            available_dir_name = candidate_dir_name

    return available_dir_name