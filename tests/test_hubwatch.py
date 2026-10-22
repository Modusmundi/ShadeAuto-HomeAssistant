"""Unit tests for the pure parts of tools/hubwatch.py."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tools import hubwatch

from .conftest import KITCHEN, load_json


def test_parse_kv_accepts_ints_and_rejects_everything_else() -> None:
    assert hubwatch.parse_kv(["MiddleRailPosition=50", "MotorStop=170"]) == {
        "MiddleRailPosition": 50,
        "MotorStop": 170,
    }
    assert hubwatch.parse_kv([]) == {}
    for bad in ("MiddleRailPosition", "=5", "Key=", "Key=abc"):
        with pytest.raises(ValueError):
            hubwatch.parse_kv([bad])


def test_build_control_payload_matches_the_integration() -> None:
    payload = hubwatch.build_control_payload(KITCHEN, {"MiddleRailPosition": 50}, "thing", now=1_700_000_000.5)
    assert payload == {
        "PeripheralUID": KITCHEN,
        "TaskID": int(1_700_000_000.5 * 1000) & 0x7FFFFFFF,
        "Timestamp": 1_700_000_000,
        "ThingName": "thing",
        "MiddleRailPosition": 50,
    }
    assert "ThingName" not in hubwatch.build_control_payload(KITCHEN, {}, None)


def test_snapshot_and_diff_track_only_requested_fields() -> None:
    status = load_json("status")
    before = hubwatch.snapshot(status, ("BottomRailPosition", "MiddleRailPosition"))
    assert set(before) == {14500, 23500, 23388, 12177, 3190}
    assert before[KITCHEN] == {"BottomRailPosition": 0, "MiddleRailPosition": 50}

    only_kitchen = hubwatch.snapshot(status, ("BottomRailPosition",), uids=[KITCHEN])
    assert list(only_kitchen) == [KITCHEN]

    for record in status["Peripherals"]:
        if record["PeripheralUID"] == KITCHEN:
            record["MiddleRailPosition"] = 0
            record["BatteryVoltage"] = 90  # not tracked -> not reported
    after = hubwatch.snapshot(status, ("BottomRailPosition", "MiddleRailPosition"))
    assert hubwatch.diff_snapshots(before, after) == [(KITCHEN, "MiddleRailPosition", 50, 0)]
    assert hubwatch.diff_snapshots(after, after) == []


def test_format_change_is_compact_and_stable() -> None:
    when = datetime(2026, 10, 22, 12, 1, 3, 412_000)
    line = hubwatch.format_change(when, 11.0, KITCHEN, "Kitchen", "MiddleRailPosition", 100, 0)
    assert line == "12:01:03.412 + 11.0s  12177 Kitchen            MiddleRailPosition 100 -> 0"
    first = hubwatch.format_change(when, None, KITCHEN, None, "BottomRailPosition", 0, 40)
    assert first.endswith("12177                    BottomRailPosition 0 -> 40")


class _FakeHub(hubwatch.Hub):
    """Hub whose network calls are replaced by canned answers."""

    def __init__(self) -> None:
        super().__init__("example.invalid")
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, payload: dict, *, timeout: float | None = None) -> Any:
        self.posts.append((path, payload))
        if path == "registration":
            return load_json("registration")
        if path == "GetAllPeripheral":
            return load_json("get_all_peripheral")
        if path == "control":
            return {"status": {"code": 0, "error": ""}}
        if path == "status":
            return load_json("status")
        raise AssertionError(path)


def test_main_sends_control_and_can_exit_without_watching(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    fake = _FakeHub()
    monkeypatch.setattr(hubwatch, "Hub", lambda host, port: fake)

    rc = hubwatch.main(["--host", "h", "--names", "--control", str(KITCHEN), "MiddleRailPosition=50", "--no-watch"])

    assert rc == 0
    paths = [path for path, _ in fake.posts]
    assert paths == ["registration", "GetAllPeripheral", "control"]
    control = fake.posts[-1][1]
    assert control["PeripheralUID"] == KITCHEN
    assert control["MiddleRailPosition"] == 50
    assert control["ThingName"] == load_json("registration")["ThingName"]
    out = capsys.readouterr().out
    assert "12177: Kitchen" in out
    assert "control 12177 {'MiddleRailPosition': 50}" in out


def test_main_watch_prints_initial_snapshot_then_changes(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    fake = _FakeHub()
    second = load_json("status")
    for record in second["Peripherals"]:
        if record["PeripheralUID"] == KITCHEN:
            record["TargetMiddleRailPosition"] = 0
    polls = iter([load_json("status"), second])
    monkeypatch.setattr(fake, "status", lambda: next(polls))
    monkeypatch.setattr(hubwatch, "Hub", lambda host, port: fake)
    monkeypatch.setattr(hubwatch.time, "sleep", lambda _s: None)

    # monotonic() calls: deadline, loop check, loop check, last_change stamp, loop check (past deadline).
    clock = iter([0.0, 0.0, 0.0, 0.5, 5.0])
    monkeypatch.setattr(hubwatch.time, "monotonic", lambda: next(clock, 10.0))

    rc = hubwatch.main(["--host", "h", "--duration", "1", "--uid", str(KITCHEN)])

    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[1:3] == [str(KITCHEN), "BottomRailPosition=0"]
    assert any("TargetMiddleRailPosition 50 -> 0" in line for line in out)
