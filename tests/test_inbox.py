"""The web slicer add-on's inbox service (addon/bambustudio_web/.../os2slice-inbox):
its watchdog backoff, without starting any process."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "addon/bambustudio_web/root/usr/local/bin/os2slice-inbox"


@pytest.fixture(scope="module")
def inbox() -> ModuleType:
    loader = SourceFileLoader("os2slice_inbox", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)  # defines things only; main() runs under __main__
    return module


def test_quick_exits_back_off_after_three(inbox: ModuleType) -> None:
    b = inbox.Backoff()
    assert b.may_launch(0) and b.fields() == {} and b.delay() == 0
    # The first two quick exits keep the normal pace and say nothing.
    assert b.exited(255, 0.3, 1000) is None
    assert b.exited(255, 0.3, 1060) is None
    assert not b.failing and b.fields() == {} and b.may_launch(1061)
    # The third: failing, one line for the supervisor log, a 60 s wait.
    line = b.exited(255, 0.3, 1120)
    assert line == (
        "os2slice-inbox: Bambu Studio exited with status 255 after 0.3 s, "
        "3 times in a row; next try in 1 min"
    )
    assert b.failing and not b.may_launch(1179) and b.may_launch(1180)
    assert b.fields() == {
        "failing": True,
        "last_exit": 255,
        "failures": 3,
        "failing_since": 1000,
        "next_try": 1180,
    }
    # Then doubling, up to 30 min.
    assert "4 times in a row; next try in 2 min" in b.exited(255, 0.3, 1200)
    assert b.next_try == 1320
    delays = []
    for i in range(10):
        b.exited(255, 0.3, 2000 + i)
        delays.append(b.delay())
    assert delays[:4] == [240, 480, 960, 1800] and set(delays[3:]) == {1800}
    assert b.fields()["failing_since"] == 1000  # the streak's start


def test_backoff_logs_once_per_delay(inbox: ModuleType) -> None:
    b = inbox.Backoff()
    lines = [b.exited(255, 0.3, float(t)) for t in range(12)]
    said = [line for line in lines if line]
    # Failing (1 min), then 2, 4, 8, 16, 30 min; the cap is said once.
    assert len(said) == 6 and said[-1].endswith("next try in 30 min")


def test_survivor_resets(inbox: ModuleType) -> None:
    b = inbox.Backoff()
    for t in range(4):
        b.exited(255, 0.3, float(t))
    assert b.failing
    assert b.exited(0, 3600, 5000) is None  # ran for an hour, then closed
    assert not b.failing and b.failures == 0 and b.fields() == {} and b.may_launch(5000)
    b.exited(-6, 1.0, 6000)  # a new streak starts from scratch
    assert b.failures == 1 and b.last_exit == -6 and b.failing_since == 6000


def test_reset_clears_a_streak(inbox: ModuleType) -> None:
    b = inbox.Backoff()
    for t in range(5):
        b.exited(1, 0.1, float(t))
    b.reset()  # e.g. the image's autostart got it running
    assert not b.failing and b.may_launch(0) and b.fields() == {}
    # After a reset the "failing" line is said again for a new streak.
    assert [b.exited(1, 0.1, 10.0 + t) for t in range(3)][-1] is not None
