"""Firestore Store (production).

Documents and their encoding come from `tifa.store.base`, shared with the in-memory store. Each poll is one
Firestore transaction: it reads `meta/state`, refuses slots already recorded, and writes the state, months, poll
times, failed slot and snapshot together. Firestore retries a transaction that loses to a concurrent one, so
`fn` may run more than once.

The client honours `FIRESTORE_EMULATOR_HOST`: when it is set, it talks to the emulator without credentials.
"""

from __future__ import annotations

import copy
import random
import time
from collections.abc import Callable
from typing import Any

from google.api_core import exceptions as gexc
from google.cloud import firestore

from tifa.store.base import (
    APPLIED,
    DUPLICATE,
    GUARD_DOC,
    STATE_DOC,
    MonthDoc,
    Plan,
    PollsDoc,
    PollWrite,
    State,
    check_day,
    check_doc_id,
    check_month,
    checked_doc,
    is_duplicate,
    month_from_doc,
    plan_months,
    plan_poll,
    polls_from_doc,
    snapshot_doc,
    snapshot_from_doc,
    state_from_doc,
)

COMMIT_ROUNDS = 3  # rounds of the client's own retries before giving up; a round is max_attempts tries


def _contended(exc: BaseException) -> bool:
    """A transaction that lost to concurrent writers (the client's retries ran out), not a real error."""
    if isinstance(exc, gexc.Aborted):
        return True
    return isinstance(exc, ValueError) and "Failed to commit transaction" in str(exc)


def _with_rounds(attempt: Callable[[], Any]) -> Any:
    """Run a transaction, starting it afresh after a short random pause if contention exhausted its retries."""
    for round_no in range(COMMIT_ROUNDS):
        try:
            return attempt()
        except Exception as exc:
            if not _contended(exc) or round_no == COMMIT_ROUNDS - 1:
                raise
            time.sleep(random.uniform(0.05, 0.25) * (round_no + 1))
    raise AssertionError("unreachable")


class FirestoreStore:
    def __init__(
        self,
        client: firestore.Client | None = None,
        *,
        project: str | None = None,
        database: str | None = None,
        max_attempts: int = 10,
    ) -> None:
        """`project` defaults to the environment's (GOOGLE_CLOUD_PROJECT or the runtime's), `database` to
        "(default)". `max_attempts` bounds the retries of a transaction under contention."""
        self._db = client or firestore.Client(project=project, database=database)
        self._max_attempts = max_attempts

    def _doc(self, path: str) -> firestore.DocumentReference:
        return self._db.document(path)

    def _read(self, path: str, transaction: firestore.Transaction | None = None) -> dict[str, Any] | None:
        snap = self._doc(path).get(transaction=transaction)
        return snap.to_dict() if snap.exists else None

    def _state(self, transaction: firestore.Transaction | None = None) -> State | None:
        doc = self._read(STATE_DOC, transaction)
        return state_from_doc(doc) if doc is not None else None

    def get_state(self) -> State | None:
        return self._state()

    def poll_transaction(self, slot: int, fn: Callable[[State | None], PollWrite | None]) -> str:
        @firestore.transactional
        def run(transaction: firestore.Transaction) -> str:
            current = self._state(transaction)
            if is_duplicate(current, slot):
                return DUPLICATE
            write = fn(copy.deepcopy(current))
            if write is None:
                return DUPLICATE
            self._apply(transaction, plan_poll(current, write, int(time.time())))
            return APPLIED

        return _with_rounds(lambda: run(self._db.transaction(max_attempts=self._max_attempts)))

    def _apply(self, transaction: firestore.Transaction, plan: Plan) -> None:
        if plan.state is not None:
            transaction.set(self._doc(STATE_DOC), plan.state)
        for month, doc in plan.months.items():
            transaction.set(self._doc(f"months/{month}"), doc)
        for day, (times, failed) in plan.polls.items():
            change: dict[str, Any] = {}
            if times:
                change |= {"at": firestore.ArrayUnion(times), "n": firestore.Increment(len(times))}
            if failed:
                change["failed"] = firestore.ArrayUnion(failed)
            transaction.set(self._doc(f"polls/{day}"), change, merge=True)
        if plan.snapshot is not None:
            ts, doc = plan.snapshot
            transaction.set(self._doc(f"snapshots/{ts}"), doc)

    def get_month(self, month: str) -> MonthDoc | None:
        doc = self._read(f"months/{check_month(month)}")
        return month_from_doc(doc) if doc is not None else None

    def list_months(self) -> dict[str, int]:
        state = self._state()
        return {m: v["rev"] for m, v in sorted(state.months.items())} if state else {}

    def put_month(self, month: str, payload: dict) -> int:
        check_month(month)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> int:
            state, docs = plan_months(self._state(transaction), None, {month: payload}, int(time.time()))
            transaction.set(self._doc(STATE_DOC), state)
            transaction.set(self._doc(f"months/{month}"), docs[month])
            return docs[month]["rev"]

        return _with_rounds(lambda: run(self._db.transaction(max_attempts=self._max_attempts)))

    def get_polls(self, day: str) -> PollsDoc | None:
        doc = self._read(f"polls/{check_day(day)}")
        return polls_from_doc(day, doc) if doc is not None else None

    def add_snapshot(self, ts: int, payload: dict) -> None:
        self._doc(f"snapshots/{int(ts)}").set(snapshot_doc(payload))

    def get_snapshot(self, ts: int) -> dict | None:
        doc = self._read(f"snapshots/{int(ts)}")
        return snapshot_from_doc(doc) if doc is not None else None

    def get_guard(self) -> dict | None:
        return self._read(GUARD_DOC)

    def put_guard(self, doc: dict) -> None:
        self._doc(GUARD_DOC).set(checked_doc(doc, GUARD_DOC))

    def record_import(self, import_id: str, doc: dict) -> None:
        path = f"imports/{check_doc_id(import_id)}"
        self._doc(path).set(checked_doc(doc, path))

    def get_import(self, import_id: str) -> dict | None:
        return self._read(f"imports/{check_doc_id(import_id)}")
