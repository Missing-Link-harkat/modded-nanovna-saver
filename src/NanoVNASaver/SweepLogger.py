from datetime import datetime
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
LOG_ROOT = os.path.join(BASE_DIR, "segmented_sweeps")

_current_file = None
_current_dir = None


def start_log(sweep_name, settings_dict):

    global _current_file
    global _current_dir

    os.makedirs(LOG_ROOT, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    safe_name = sweep_name.replace(" ", "_")

    folder = f"{timestamp}_{safe_name}"
    _current_dir = os.path.join(LOG_ROOT, folder)

    os.makedirs(_current_dir, exist_ok=True)

    points_path = os.path.join(_current_dir, "points.csv")

    _current_file = open(points_path, "w")
    _current_file.write("frequency_hz,vswr\n")

    settings_path = os.path.join(_current_dir, "settings.csv")

    with open(settings_path, "w") as f:
        f.write("parameter,value\n")

        for key, value in settings_dict.items():
            f.write(f"{key},{value}\n")

        f.write(f"timestamp,{datetime.now()}\n")


def log_points(freqs, vswrs):

    global _current_file

    if _current_file is None:
        return

    for f, v in zip(freqs, vswrs):
        _current_file.write(f"{f},{v}\n")


def stop_log():

    global _current_file

    if _current_file:
        _current_file.close()
        _current_file = None
