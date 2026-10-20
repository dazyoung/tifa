"""A differential check against an independent implementation of the recorder, when one is available.

Set TIFA_REFERENCE_DIR to a directory holding it (or to its Python file). It must be a module with a direct mode,
``update_history(state_dir, raw, now_ts)``, that records one TfL answer per call and keeps month files in Tifa's month
format under ``<state_dir>/<TIMELINE_DIR>/`` (with ``TIMELINE_INDEX``, the months index, beside them) and a
per-line report in ``<state_dir>/<HISTORY>``. The same TfL answers, at the same poll times as Tifa's successful polls,
go through it; its months, index and line reports must match what Tifa serves. Without the variable the test is
skipped.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.integration import scenario as sc
from tests.integration.conftest import BASE, make_api
from tifa.core.tfl import current

ENV = "TIFA_REFERENCE_DIR"
ENTRY = "def update_history("


def reference_module():
    where = os.environ.get(ENV)
    if not where:
        pytest.skip(f"{ENV} is not set")
    path = Path(where).expanduser()
    if path.is_dir():
        found = [p for p in sorted(path.glob("*.py")) if ENTRY in p.read_text(encoding="utf-8", errors="replace")]
        if len(found) != 1:
            pytest.fail(f"{ENV} must hold exactly one module defining update_history(); found {len(found)}")
        path = found[0]
    if not path.is_file():
        pytest.fail(f"{ENV} is neither a directory nor a file")
    spec = importlib.util.spec_from_file_location("reference_recorder", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    for name in ("update_history", "current", "TIMELINE_DIR", "TIMELINE_INDEX", "HISTORY"):
        if not hasattr(module, name):
            pytest.fail(f"the reference module has no {name}")
    return module


@pytest.fixture(scope="module")
def reference(world, tmp_path_factory):
    """The reference's state directory after the same polls as Tifa's."""
    ref = reference_module()
    state_dir = tmp_path_factory.mktemp("reference") / "state"
    state_dir.mkdir()
    for t in world.polls:
        ref.update_history(state_dir, sc.payload(t), t)
    return ref, state_dir


def read(ref, state_dir: Path, *parts: str):
    return json.loads(state_dir.joinpath(*parts).read_text(encoding="utf-8"))


def test_months_match_the_reference(world, reference):
    ref, state_dir = reference
    theirs = read(ref, state_dir, ref.TIMELINE_DIR, "2026-10.json")
    index = read(ref, state_dir, ref.TIMELINE_DIR, ref.TIMELINE_INDEX)
    app, _, _ = make_api(world.store, sc.NOW)
    with TestClient(app, base_url=BASE) as http:
        month = http.get("/v1/months/2026-10").json()["data"]
        mine = http.get("/v1/months").json()["data"]
    assert [row[:8] for row in month["episodes"]] == theirs["episodes"]  # the established 8 fields, open rows too
    assert month["watched"]["a"] == theirs["watched"]["a"] and theirs["watched"].get("f") == []
    assert (mine["months"], mine["first"], mine["categories"], mine["causes"]) == (
        index["months"],
        index["first"],
        index["categories"],
        index["causes"],
    )
    strip = [{k: x[k] for k in ("id", "name", "mode")} for x in index["lines"]]
    assert [{k: x[k] for k in ("id", "name", "mode")} for x in mine["lines"]] == strip


def test_status_and_line_reports_match_the_reference(world, reference):
    ref, state_dir = reference
    last = sc.payload(world.polls[-1])
    assert current(last) == ref.current(last)
    history = read(ref, state_dir, ref.HISTORY)
    theirs = {x["id"]: x for x in history["lines"]}
    app, _, _ = make_api(world.store, world.polls[-1])  # as of the last poll, when the reference reported
    with TestClient(app, base_url=BASE) as http:
        for line, _, _ in sc.LINES:
            mine = http.get(f"/v1/lines/{line}").json()["data"]
            for key in ("name", "mode", "stats", "starts_by_hour"):
                assert mine[key] == theirs[line][key], (line, key)
            for key in ("now", "recent"):  # Tifa adds each episode's cause
                assert [{k: v for k, v in e.items() if k != "cause"} for e in mine[key]] == theirs[line][key], (
                    line,
                    key,
                )
