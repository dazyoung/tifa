"""tools/import_foi.py: the re-use permission gate, and source "f" months beside the live recording's."""

import calendar
import io
import json

import pytest

from tifa.core.causes import cause
from tifa.core.months import new_month
from tifa.core.timeutil import day_bounds, iso
from tifa.store.memory import MemoryStore
from tools import import_foi as foi

JAN = calendar.timegm((2024, 1, 1, 0, 0, 0))
FEB = calendar.timegm((2024, 2, 1, 0, 0, 0))
MAR = calendar.timegm((2024, 3, 1, 0, 0, 0))
HOUR = 3600


def record(line, name, sev, status, cat, planned, start, end, reason):
    return {
        "line": line,
        "name": name,
        "mode": "tube",
        "severity": sev,
        "status": status,
        "category": cat,
        "planned": planned,
        "start": iso(start),
        "end": iso(end),
        "end_estimated": False,
        "reason": reason,
        "rows": 2,  # extra fields of the archive format are ignored
        "source": "an-foi-release",
    }


RECORDS = [
    record("central", "Central", 9, "Minor Delays", "delays", False, JAN + 10 * HOUR, JAN + 11 * HOUR,
           "Minor delays due to an earlier signal failure at Bank."),
    record("circle-hammersmith-city", "Circle and Hammersmith & City", 6, "Severe Delays", "delays", False,
           FEB - HOUR, FEB + HOUR, "Severe delays due to a customer incident at Baker Street."),  # over a month end
    record("district", "District", 4, "Planned Closure", "closure", True, FEB + 50 * HOUR, FEB + 60 * HOUR,
           "No service between Earl's Court and Wimbledon while we carry out improvement works."),
]  # fmt: skip


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "archive.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in RECORDS))
    coverage = tmp_path / "coverage.json"
    coverage.write_text(json.dumps({"version": 1, "sources": [{"from": iso(JAN), "to": iso(MAR - 1)}]}))
    return path, coverage


def run(args, store, now=MAR + 86400):
    out, err = io.StringIO(), io.StringIO()
    code = foi.main([str(a) for a in args], store=store, now=now, out=out, err=err)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


def granted(path, coverage, *extra):
    return [path, "--coverage", coverage, foi.FLAG, *extra]


def test_refuses_without_the_reuse_permission_flag(archive):
    path, coverage = archive
    store = MemoryStore()
    code, report, err = run([path, "--coverage", coverage], store)
    assert code == foi.EXIT_REFUSED and report == {"ok": False, "refused": "no re-use permission confirmed"}
    assert "not a licence" in err and foi.FLAG in err
    assert store.get_state() is None and store.list_months() == {}


def test_help_explains_the_licence_position(capsys):
    with pytest.raises(SystemExit) as exit_:
        foi.build_parser().parse_args(["--help"])
    assert exit_.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "Freedom of Information" in text and "not a licence to re-use" in text
    assert "TIFA_PUBLIC_SOURCES" in text and foi.FLAG in text


def test_imports_source_f_months_with_causes_and_coverage(archive):
    path, coverage = archive
    store = MemoryStore()
    code, report, _ = run(granted(path, coverage, "--permission-ref", "licence letter 1"), store)
    assert code == 0 and report["ok"] and report["source"] == "f"
    assert {m: v["episodes"] for m, v in report["months"].items()} == {"2024-01": 2, "2024-02": 2}
    jan, feb = store.get_month("2024-01").payload, store.get_month("2024-02").payload
    assert jan["watched"] == {"a": [], "f": [[JAN, FEB]]} and feb["watched"]["f"] == [[FEB, MAR]]
    assert [(row[0], row[6], row[7]) for row in jan["episodes"]] == [
        ("central", "f", "signal"),
        ("circle-hammersmith-city", "f", "customer"),
    ]
    district = feb["episodes"][-1]
    assert district[0] == "district" and district[5] == 1 and district[7] == cause(RECORDS[2]["reason"], True)
    doc = store.get_import(report["import_id"])
    assert doc["kind"] == "foi" and doc["reuse_permission"] is True and doc["permission_ref"] == "licence letter 1"
    assert doc["coverage"] == [{"from": JAN, "to": MAR}] and doc["months"] == {"2024-01": 1, "2024-02": 1}
    shared = {line["id"]: line["shared_with"] for line in doc["lines"]}
    assert shared["circle-hammersmith-city"] == ["circle", "hammersmith-city"] and shared["central"] is None
    assert store.get_state().last_poll is None  # an archive is not a recording: import_jsonl may still follow


def test_a_rerun_changes_nothing(archive):
    path, coverage = archive
    store = MemoryStore()
    run(granted(path, coverage), store)
    before = {m: (store.get_month(m).rev, store.get_month(m).sha256) for m in store.list_months()}
    code, report, _ = run(granted(path, coverage), store)
    assert code == 0 and not any(v["changed"] for v in report["months"].values())
    assert {m: (store.get_month(m).rev, store.get_month(m).sha256) for m in store.list_months()} == before


def test_the_live_recordings_episodes_and_spans_are_kept(archive):
    path, coverage = archive
    store = MemoryStore()
    live = new_month("2024-02")
    live["episodes"] = [["victoria", FEB + 5 * HOUR, FEB + 6 * HOUR, 9, "delays", 0, "a", "signal", "Minor Delays",
                         "Minor delays due to a signal failure.", 0]]  # fmt: skip
    live["watched"]["a"] = [[FEB + 4 * HOUR, FEB + 8 * HOUR]]
    store.put_month("2024-02", live)
    assert run(granted(path, coverage), store)[0] == 0
    feb = store.get_month("2024-02").payload
    assert [row[0] for row in feb["episodes"] if row[6] == "a"] == ["victoria"]
    assert feb["watched"]["a"] == [[FEB + 4 * HOUR, FEB + 8 * HOUR]] and feb["watched"]["f"] == [[FEB, MAR]]


def test_a_new_archive_replaces_the_old_ones_episodes_and_months(archive, tmp_path):
    path, coverage = archive
    store = MemoryStore()
    run(granted(path, coverage), store)
    smaller = tmp_path / "smaller.jsonl"
    smaller.write_text(json.dumps(RECORDS[0]) + "\n")
    january = tmp_path / "january.json"
    january.write_text(json.dumps({"sources": [{"from": iso(JAN), "to": iso(FEB - 1)}]}))
    code, report, _ = run(granted(smaller, january), store)
    assert code == 0
    jan, feb = store.get_month("2024-01").payload, store.get_month("2024-02").payload
    assert [row[0] for row in jan["episodes"]] == ["central"]
    assert feb["episodes"] == [] and feb["watched"]["f"] == [] and report["months"]["2024-02"]["changed"]


def test_from_and_to_are_london_days_both_included(archive):
    path, _ = archive
    store = MemoryStore()
    code, report, _ = run([path, "--from", "2024-01-01", "--to", "2024-02-29", foi.FLAG], store)
    assert code == 0
    assert report["archive"]["from"] == iso(day_bounds("2024-01-01")[0])
    assert report["archive"]["to"] == iso(day_bounds("2024-02-29")[1])


@pytest.mark.parametrize(
    "extra",
    [[], ["--coverage", "c.json", "--from", "2024-01-01"], ["--from", "2024-01-01"], ["--to", "2024-01-01"]],
)
def test_coverage_comes_from_one_place(archive, extra):
    path, _ = archive
    code, _, err = run([path, foi.FLAG, *extra], MemoryStore())
    assert code == foi.EXIT_USAGE and "--coverage" in err


def test_bad_dates_and_empty_archives_are_bad_input(archive, tmp_path):
    path, coverage = archive
    assert run([path, "--from", "2024-02-01", "--to", "2024-01-01", foi.FLAG], MemoryStore())[0] == foi.EXIT_INPUT
    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"line": "central"}\n')
    code, _, err = run(granted(empty, coverage), MemoryStore())
    assert code == foi.EXIT_INPUT and "no readable episodes" in err


def test_dry_run_writes_nothing(archive):
    path, coverage = archive
    store = MemoryStore()
    code, report, _ = run(granted(path, coverage, "--dry-run"), store)
    assert code == 0 and report["dry_run"] and set(report["months"]) == {"2024-01", "2024-02"}
    assert store.list_months() == {} and store.get_import(report["import_id"]) is None


def test_the_store_must_be_named_and_memory_is_never_the_default(archive, monkeypatch):
    path, coverage = archive
    for name in ("TIFA_STORE", "GOOGLE_CLOUD_PROJECT", "TIFA_FIRESTORE_DATABASE", "FIRESTORE_EMULATOR_HOST"):
        monkeypatch.delenv(name, raising=False)

    def main(*args):
        out, err = io.StringIO(), io.StringIO()
        return foi.main([str(a) for a in granted(path, coverage, *args)], now=MAR + 86400, out=out, err=err), err

    for env in ({}, {"TIFA_STORE": "memory"}):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        code, err = main()
        assert code == foi.EXIT_USAGE and "--store firestore" in err.getvalue()
    code, err = main("--store", "memory")
    assert code == 0 and "keeps nothing" in err.getvalue()
