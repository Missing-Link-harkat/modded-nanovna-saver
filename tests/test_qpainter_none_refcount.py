"""QPainter calls must not leak references to None (#22).

PySide6 6.12.0 drops a reference to None on every QPainter.drawPoint and
drawLine. Before Python 3.12 None is a normal refcounted object, so the charts
(which draw point by point) eventually deallocate it and the process aborts
with "Fatal Python error: none_dealloc". pyproject.toml excludes 6.12.0 for
Python < 3.12; this test catches the next PySide6 release with the same bug.
From 3.12 on None is immortal and its refcount does not move, so the test
passes there whatever PySide6 does.
"""

import sys

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

CALLS = 1000


@pytest.fixture(scope="module")
def painter():
    _ = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    image = QtGui.QImage(100, 100, QtGui.QImage.Format.Format_RGB32)
    qp = QtGui.QPainter(image)
    yield qp
    qp.end()


@pytest.mark.parametrize("call", ("drawPoint", "drawLine"))
def test_no_none_leak(painter, call):
    p, q = QtCore.QPoint(5, 5), QtCore.QPoint(50, 50)
    draw = getattr(painter, call)
    args = (p,) if call == "drawPoint" else (p, q)
    before = sys.getrefcount(None)
    for _ in range(CALLS):
        draw(*args)
    lost = before - sys.getrefcount(None)
    # a leaking build loses one per call; allow a few for unrelated objects
    assert lost < CALLS // 10, (
        f"QPainter.{call} lost {lost} references to None in {CALLS} calls"
        " (#22: exclude this PySide6 version for Python < 3.12)"
    )
