"""Loaded by every Saver the evaluation starts (run_eval.py puts this
folder first on PYTHONPATH): no serial ports, so a run with the virtual
device never opens or probes a real VNA that happens to be plugged in."""

import serial.tools.list_ports as _list_ports

_list_ports.comports = lambda *args, **kwargs: []
