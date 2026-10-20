"""tools/import_jsonl.py against a synthetic recording in the local recorder's format."""

import io
import json

import pytest

from tests.tools import recorder as r
from tifa.core.causes import CAUSES_SIG
from tifa.core.episodes import advance, observe
from tifa.core.months import extend_run, merge_month, new_month, run_segment, split_months
from tifa.core.tfl import current
from tifa.core.timeutil import iso, utc_day
from tifa.store import codec
from tifa.store.base import PollWrite, State
from tifa.store.memory import MemoryStore
from tools import import_jsonl as ij

MIN = 60
NOW = r.LAST_POLL + 30


def run(args, store, now=NOW):
    out, err = io.StringIO(), io.StringIO()
    code = ij.main([str(a) for a in args], store=store, now=now, out=out, err=err)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


def payloads(store):
    return {m: store.get_month(m).payload for m in store.list_months()}


def everything(store):
    """Every document the import writes, for comparing a store before and after."""
    state = store.get_state()
    polls = {d: store.get_polls(d) for d in ("2026-09-30", "2026-10-01")}
    return (
        state.to_dict() if state else None,
        {m: (doc.rev, doc.sha256) for m in store.list_months() for doc in [store.get_month(m)]},
        {d: (p.n, p.at) if p else None for d, p in polls.items()},
    )


def poll_by_poll(polls):
    """Month payloads as a poller recording these polls would build them (the recipe in tifa.core.months)."""
    state, run_, months = {"last_poll": None, "open": []}, None, {}
    for ts in polls:
        obs, meta = observe(r.payload(ts))
        state, closed, _ = advance(state, obs, meta, ts)
        prev, run_ = run_, extend_run(run_, ts)
        piece = run_segment(prev, run_)
        if piece is not None:
            for name in split_months(*piece):
                months[name] = merge_month(months.get(name) or new_month(name), (), [piece])
        for ep in closed:
            for name in split_months(ep["start"], ep["end"]):
                months[name] = merge_month(months.get(name) or new_month(name), [ep])
    return months


# ---------------------------------------------------------------- reading


def test_reads_the_recording(recording):
    rec = ij.read_recording(recording, recording / "tfl.json")
    assert [(e["line"], e["end_estimated"]) for e in rec.closed] == [
        ("central", False),
        ("northern", False),
        ("district", True),  # ended at the last poll before the 30-minute gap
    ]
    assert [e["line"] for e in rec.open] == ["district", "jubilee"]
    assert rec.polls == r.LOCAL_POLLS and rec.last_poll == r.LAST_POLL
    assert rec.runs() == [[r.T0, r.T0 + 240 * MIN], [r.T0 + 270 * MIN, r.LAST_POLL]]
    assert rec.status == current(r.payload(r.LAST_POLL)) and rec.status_fetched_at == r.LAST_POLL
    assert rec.skipped == {}
    assert set(rec.sha256) == {"episodes.jsonl", "polls.jsonl", "open.json", "tfl.json"}


def test_cut_short_and_repeated_lines_are_skipped_and_counted(recording):
    episodes = recording / "episodes.jsonl"
    first = episodes.read_text().splitlines()[0]
    episodes.write_text(episodes.read_text() + first + "\n" + '{"line":"central","sev')  # a repeat, a crash
    (recording / "polls.jsonl").write_text((recording / "polls.jsonl").read_text() + '{"at":"2026-10-01T03:0')
    rec = ij.read_recording(recording)
    assert len(rec.closed) == 3
    assert rec.skipped == {"episodes_repeated": 1, "episodes_unreadable": 1, "polls_unreadable": 1}


def test_polls_after_open_jsons_last_poll_are_left_out(recording):
    later = json.dumps({"at": "2026-10-01T03:02:00+00:00"})
    (recording / "polls.jsonl").write_text((recording / "polls.jsonl").read_text() + later + "\n")
    rec = ij.read_recording(recording)
    assert rec.polls[-1] == r.LAST_POLL and rec.skipped == {"polls_after_last_poll": 1}


def closed_record(recording, line, end, estimated=False):
    """An episodes.jsonl line closing the episode open.json holds open for this line, as the next poll writes it."""
    entry = next(e for e in json.loads((recording / "open.json").read_text())["open"] if e["line"] == line)
    return json.dumps({**entry, "end": iso(end), "end_estimated": estimated}) + "\n"


def test_episodes_closed_after_open_jsons_last_poll_stay_open(recording):
    """A crash after appending to episodes.jsonl and before saving open.json: the episodes are imported open, as
    open.json has them, and the poller carries them on, once."""
    episodes = recording / "episodes.jsonl"
    extra = closed_record(recording, "jubilee", r.LAST_POLL + 60)  # closed by a poll open.json never saw
    extra += closed_record(recording, "district", r.LAST_POLL, estimated=True)  # a gap's closure at the last poll
    episodes.write_text(episodes.read_text() + extra)
    rec = ij.read_recording(recording)
    assert [e["line"] for e in rec.closed] == ["central", "northern", "district"]
    assert rec.closed[-1]["end"] == r.T0 + 240 * MIN  # the district episode before the gap, not the open one
    assert [e["line"] for e in rec.open] == ["district", "jubilee"]
    assert rec.skipped == {"episodes_after_last_poll": 2}


def test_a_poll_during_the_import_is_left_out_whole(recording, monkeypatch):
    """open.json is read first: a recorder poll that lands while the files are read only adds what comes after
    its last poll, so an episode that poll closed is neither lost nor imported twice."""
    later = r.LAST_POLL + 60
    read, reads = ij._read, []

    def recorder_polls_after_the_first_read(path):
        data = read(path)
        reads.append(path.name)
        if len(reads) == 1:
            with open(recording / "episodes.jsonl", "a") as fh:
                fh.write(closed_record(recording, "jubilee", later))
            with open(recording / "polls.jsonl", "a") as fh:
                fh.write(json.dumps({"at": iso(later)}) + "\n")
            doc = json.loads((recording / "open.json").read_text())
            doc.update(last_poll=iso(later), open=[e for e in doc["open"] if e["line"] != "jubilee"])
            (recording / "open.json").write_text(json.dumps(doc))
        return data

    monkeypatch.setattr(ij, "_read", recorder_polls_after_the_first_read)
    rec = ij.read_recording(recording)
    assert reads[0] == "open.json"
    assert rec.last_poll == r.LAST_POLL and rec.polls[-1] == r.LAST_POLL
    assert [e["line"] for e in rec.open] == ["district", "jubilee"]
    assert "jubilee" not in [e["line"] for e in rec.closed]
    assert rec.skipped == {"episodes_after_last_poll": 1, "polls_after_last_poll": 1}


@pytest.mark.parametrize("name", ["episodes.jsonl", "polls.jsonl", "open.json"])
def test_a_missing_file_is_bad_input(recording, name):
    (recording / name).unlink()
    code, report, err = run([recording], MemoryStore())
    assert code == ij.EXIT_INPUT and report is None and name in err


def test_an_open_json_without_a_last_poll_is_bad_input(recording):
    (recording / "open.json").write_text(json.dumps({"version": 1, "last_poll": None, "open": []}))
    code, _, err = run([recording], MemoryStore())
    assert code == ij.EXIT_INPUT and "last_poll" in err


# ---------------------------------------------------------------- importing


def test_imports_months_polls_state_and_an_audit_record(recording):
    store = MemoryStore()
    code, report, _ = run([recording, "--status", recording / "tfl.json"], store)
    assert code == 0 and report["ok"] and not report["dry_run"]
    assert report["months"]["2026-09"] == {"episodes": 3, "watched_s": 7200, "changed": True, "rev": 1}
    assert report["months"]["2026-10"] == {"episodes": 2, "watched_s": 9000, "changed": True, "rev": 1}

    sept, octo = store.get_month("2026-09").payload, store.get_month("2026-10").payload
    assert [(row[0], row[6], row[7], row[10]) for row in sept["episodes"]] == [
        ("district", "a", "engineering", 1),
        ("central", "a", "signal", 0),
        ("northern", "a", "train", 0),
    ]
    assert [row[0] for row in octo["episodes"]] == ["district", "northern"]  # both run over midnight
    assert sept["watched"] == {"a": [[r.T0, r.T0 + 120 * MIN]], "f": []}
    assert octo["watched"]["a"] == [[r.T0 + 120 * MIN, r.T0 + 240 * MIN], [r.T0 + 270 * MIN, r.LAST_POLL]]

    polls = [store.get_polls("2026-09-30"), store.get_polls("2026-10-01")]
    assert sum(p.n for p in polls) == len(r.LOCAL_POLLS) and polls[0].at + polls[1].at == r.LOCAL_POLLS

    state = store.get_state()
    assert state.last_poll == r.LAST_POLL and state.last_slot == r.LAST_POLL // 60
    assert state.run == [r.T0 + 270 * MIN, r.LAST_POLL]
    assert [(e["line"], e["start"]) for e in state.open] == [
        ("district", r.T0 + 270 * MIN),
        ("jubilee", r.T0 + 280 * MIN),
    ]
    assert state.status == current(r.payload(r.LAST_POLL)) and state.status_etag == codec.digest(state.status)
    assert state.causes_sig == CAUSES_SIG and state.months == {
        "2026-09": {"rev": 1, "n": 3},
        "2026-10": {"rev": 1, "n": 2},
    }
    assert list(state.lines) == sorted(lid for lid, _, _ in r.LINES)
    assert state.lines["victoria"] == {"name": "Victoria", "mode": "tube", "first_seen": r.T0, "last_seen": r.LAST_POLL}
    assert state.lines["central"]["first_seen"] == r.T0 and state.lines["elizabeth"]["mode"] == "elizabeth-line"

    record = store.get_import(report["import_id"])
    assert report["import_id"].startswith("jsonl-20261001T030000Z-")
    assert record["kind"] == "jsonl" and record["last_poll"] == r.LAST_POLL and record["polls"] == len(r.LOCAL_POLLS)
    assert record["months"] == {"2026-09": 1, "2026-10": 1} and set(record["inputs"]) >= {"open.json", "tfl.json"}


def test_months_match_a_poll_by_poll_recording(recording):
    store = MemoryStore()
    assert run([recording], store)[0] == 0
    assert payloads(store) == poll_by_poll(r.LOCAL_POLLS)


def test_without_a_status_the_lines_come_from_the_episodes(recording):
    store = MemoryStore()
    assert run([recording], store)[0] == 0
    state = store.get_state()
    assert state.status == {} and state.status_etag == ""
    assert list(state.lines) == ["central", "district", "jubilee", "northern"]
    assert state.lines["district"]["last_seen"] == r.LAST_POLL  # still open


def test_the_status_may_be_tfls_raw_response_or_current(recording, tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(r.payload(r.LAST_POLL)))
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps(current(r.payload(r.LAST_POLL))))
    for path in (raw, plain):
        store = MemoryStore()
        assert run([recording, "--status", path], store)[0] == 0
        assert store.get_state().status == current(r.payload(r.LAST_POLL))


def test_a_status_from_a_failed_fetch_is_refused(recording):
    doc = json.loads((recording / "tfl.json").read_text())
    doc["ok"] = False
    (recording / "tfl.json").write_text(json.dumps(doc))
    store = MemoryStore()
    code, _, err = run([recording, "--status", recording / "tfl.json"], store)
    assert code == ij.EXIT_INPUT and "failed fetch" in err and store.get_state() is None


def test_rerun_with_force_changes_nothing(recording):
    store = MemoryStore()
    args = [recording, "--status", recording / "tfl.json"]
    first = run(args, store)
    before = everything(store)
    code, report, _ = run([*args, "--force"], store)
    assert code == 0 and before == everything(store)
    assert not any(m["changed"] for m in report["months"].values())
    assert report["polls"] == {"days": 0, "added": 0, "write": "per-poll"} and report["state"]["changed"] is False
    assert report["import_id"] == first[1]["import_id"]


def test_a_store_with_a_recording_is_refused_without_force(recording):
    store = MemoryStore()
    run([recording], store)
    before = everything(store)
    code, report, err = run([recording], store)
    assert code == ij.EXIT_REFUSED and report["ok"] is False and "--force" in err
    assert everything(store) == before


def test_a_store_holding_only_archive_months_is_not_a_recording(recording):
    store = MemoryStore()
    archived = new_month("2026-09")
    archived["episodes"] = [["central", r.T0 - 86400, r.T0 - 80000, 9, "delays", 0, "f", "signal"]]
    archived["watched"]["f"] = [[r.T0 - 86400, r.T0 - 3600]]
    store.put_month("2026-09", archived)
    code, report, _ = run([recording], store)
    assert code == 0 and report["months"]["2026-09"]["rev"] == 2
    sept = store.get_month("2026-09").payload
    assert sept["episodes"][0][6] == "f" and len(sept["episodes"]) == 4  # the archive's episode is kept
    assert sept["watched"]["f"] == [[r.T0 - 86400, r.T0 - 3600]]


def test_a_last_poll_older_than_max_age_is_refused_and_nothing_written(recording):
    store = MemoryStore()
    code, report, err = run([recording], store, now=r.LAST_POLL + 301)
    assert code == ij.EXIT_REFUSED and "301 s old" in report["refused"] and "--max-age" in err
    assert store.get_state() is None and store.list_months() == {} and store.get_polls("2026-10-01") is None
    assert run([recording, "--max-age", "400"], store, now=r.LAST_POLL + 301)[0] == 0


def test_max_age_zero_turns_the_check_off(recording):
    assert run([recording, "--max-age", "0"], MemoryStore(), now=r.LAST_POLL + 30 * 86400)[0] == 0


def test_a_last_poll_in_the_future_is_refused(recording):
    code, report, _ = run([recording], MemoryStore(), now=r.LAST_POLL - 3600)
    assert code == ij.EXIT_REFUSED and "future" in report["refused"]


def test_a_newer_state_is_never_moved_back(recording):
    store = MemoryStore()
    run([recording], store)
    later = r.LAST_POLL + 60
    store.poll_transaction(later // 60, lambda s: PollWrite(state=State(**{**s.to_dict(), "last_poll": later,
                                                                            "last_slot": later // 60})))  # fmt: skip
    before = everything(store)
    code, report, _ = run([recording, "--force"], store, now=later + 10)
    assert code == ij.EXIT_REFUSED and "never moves back" in report["refused"]
    assert everything(store) == before


def test_dry_run_writes_nothing_and_reports_the_plan(recording):
    store = MemoryStore()
    code, report, _ = run([recording, "--dry-run"], store)
    assert code == 0 and report["dry_run"]
    assert report["polls"] == {"days": 2, "added": len(r.LOCAL_POLLS), "write": "per-poll"}
    assert set(report["months"]) == {"2026-09", "2026-10"} and report["state"]["open"] == 2
    assert store.get_state() is None and store.list_months() == {}
    assert run([recording, "--dry-run"], store, now=r.LAST_POLL + 900)[0] == ij.EXIT_REFUSED  # same checks


def test_polls_days_limits_the_poll_documents_only(recording):
    early = [r.T0 - 3 * 86400, r.T0 - 3 * 86400 + 120]  # two polls three days before the rest
    lines = "".join(json.dumps({"at": iso(t)}) + "\n" for t in early)
    (recording / "polls.jsonl").write_text(lines + (recording / "polls.jsonl").read_text())
    store = MemoryStore()
    code, report, _ = run([recording, "--polls-days", "1"], store)
    assert code == 0 and report["polls"]["added"] == len(r.LOCAL_POLLS)
    assert store.get_polls(utc_day(early[0])) is None  # no poll document that far back
    assert store.get_month("2026-09").payload["watched"]["a"][0] == early  # but the watched time is there


class BulkStore(MemoryStore):
    """A store offering add_polls(day, times), as a Store may: the import then writes one call per day."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.transactions = 0

    def add_polls(self, day, times):
        self.calls.append((day, list(times)))
        for ts in times:
            super().poll_transaction(0, lambda _s, ts=ts: PollWrite(poll_at=ts))

    def poll_transaction(self, slot, fn):
        self.transactions += 1
        return super().poll_transaction(slot, fn)


def test_bulk_poll_writes_when_the_store_offers_them(recording):
    store = BulkStore()
    code, report, _ = run([recording], store)
    assert code == 0 and report["polls"]["write"] == "bulk"
    assert [day for day, _ in store.calls] == ["2026-09-30", "2026-10-01"]
    assert store.transactions == 1  # the state; the polls went through add_polls
    assert sum(len(t) for _, t in store.calls) == len(r.LOCAL_POLLS)


def test_without_add_polls_each_poll_is_its_own_transaction(recording, monkeypatch):
    code, report, err = run([recording], MemoryStore())
    assert code == 0 and report["polls"]["write"] == "per-poll" and err == ""
    monkeypatch.setattr(ij, "SLOW_POLLS", 100)
    code, report, err = run([recording], MemoryStore())
    assert code == 0 and "--polls-days" in err  # warned that it may be slow


def test_the_poller_carries_on_from_the_imported_state(recording):
    poll = pytest.importorskip("tifa.poller.poll")
    from tifa.poller.tfl_client import FetchResult

    class Fetcher:
        def __init__(self, raw):
            self.raw = raw

        def fetch_status(self):
            return FetchResult(raw=self.raw, auth="ok", status=200, size=0, elapsed_ms=1)

    store = MemoryStore()
    assert run([recording, "--status", recording / "tfl.json"], store)[0] == 0
    first = r.LAST_POLL + 60
    result = poll.run_poll(store, Fetcher(r.payload(first)), first // 60, first)
    assert result.outcome == "applied" and result.closed == 0 and result.opened == 0
    state = store.get_state()
    assert [(e["line"], e["start"]) for e in state.open] == [
        ("district", r.T0 + 270 * MIN),
        ("jubilee", r.T0 + 280 * MIN),
    ]
    assert state.run == [r.T0 + 270 * MIN, first]
    assert store.get_month("2026-10").payload["watched"]["a"][-1] == [r.T0 + 270 * MIN, first]


def test_the_store_must_be_named_and_memory_is_never_the_default(recording, monkeypatch):
    import argparse

    args = argparse.Namespace(store="memory", project=None, database=None)
    store, kind = ij.open_store(args, {})
    assert kind == "memory" and isinstance(store, MemoryStore)
    args.store = None
    for env in ({}, {"TIFA_STORE": "memory"}):
        with pytest.raises(ij.ConfigError, match="--store firestore"):
            ij.open_store(args, env)
    args.store = "firestore"
    with pytest.raises(ij.ConfigError, match="GOOGLE_CLOUD_PROJECT"):
        ij.open_store(args, {})
    args.store = None
    with pytest.raises(ij.ConfigError, match="GOOGLE_CLOUD_PROJECT"):  # TIFA_STORE=firestore names the store
        ij.open_store(args, {"TIFA_STORE": "firestore"})

    for name in ij.STORE_ENV:
        monkeypatch.delenv(name, raising=False)

    def main(*args):
        out, err = io.StringIO(), io.StringIO()
        return ij.main([str(recording), *args], out=out, err=err), err.getvalue()

    code, err = main()
    assert code == ij.EXIT_USAGE and "--store firestore" in err and "--store memory" in err
    code, err = main("--store", "firestore")
    assert code == ij.EXIT_USAGE and "GOOGLE_CLOUD_PROJECT" in err
    code, err = main("--store", "memory", "--max-age", "0")
    assert code == 0 and "keeps nothing" in err
