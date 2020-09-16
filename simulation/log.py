import datetime
import gzip
from jsonpatch import make_patch
from os import makedirs
from os.path import exists
from typing import Dict, List, Union
import ujson as json


BASE_LOG_DIR = "runlog"
FILE_ENDING = "json"
LISTING_PATH = f"{BASE_LOG_DIR}/listing.{FILE_ENDING}"

class SimulationLogger:
    def __init__(self, simulation_id: str):
        self.id = simulation_id
        self.log_dir = f"{BASE_LOG_DIR}/{self.id}"
        self._previous_state = None

        self._simulation_metadata = {
            "id": self.id,
            "tickCount": 0
        }

    def _get_file_path(self, file_short_name: str):
        return f"{self.log_dir}/{file_short_name}.{FILE_ENDING}" 

    def _write_json(self, to_write: Union[List, Dict], file_short_name: str):
        with gzip.open(
            self._get_file_path(file_short_name), "wt", encoding="utf-8"
        ) as logfile:
            json.dump(to_write, logfile, ensure_ascii=False)

    def _update_simulation_listing(self):
        listing = {
            "simulations": []
        }

        if exists(LISTING_PATH):
            with gzip.open(LISTING_PATH) as listing_file:
                listing = json.load(listing_file)

        listing["simulations"].append({
            "id": self.id,
            "index": self._get_file_path("index")
        })
        
        with gzip.open(LISTING_PATH, "wt", encoding="utf-8") as listing_file:
            json.dump(listing, listing_file)


    def log_state(self, tick: int, state: Dict):
        makedirs(self.log_dir, exist_ok=True)

        self._write_json(state, tick)

        if self._previous_state is not None:
            patch = make_patch(self._previous_state, state)
            self._write_json(patch, f"{tick-1}-{tick}.patch")

        self._simulation_metadata["tickCount"] += 1
        self._write_json(self._simulation_metadata, "index")

        if tick == 1:
            self._update_simulation_listing()

        self._previous_state = state


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
