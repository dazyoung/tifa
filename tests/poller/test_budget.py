"""tifa.poller.budget: Pub/Sub push envelopes and Cloud Billing budget notifications."""

import base64
import json

import pytest

from tests.poller.helpers import utc
from tifa.poller.budget import MAX_BODY, BudgetMessageError, BudgetNotice, parse_notification, parse_push

NOTIFICATION = {
    "budgetDisplayName": "example-budget",
    "alertThresholdExceeded": 1.0,
    "costAmount": 3.80,
    "costIntervalStart": "2026-10-01T07:00:00Z",
    "budgetAmount": 3.75,
    "budgetAmountType": "SPECIFIED_AMOUNT",
    "currencyCode": "GBP",
}


def push(data, *, urlsafe=False) -> bytes:
    raw = json.dumps(data).encode() if not isinstance(data, bytes) else data
    encoded = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(raw).decode()
    return json.dumps(
        {
            "message": {
                "data": encoded,
                "attributes": {"budgetId": "00000000-0000-0000-0000-000000000000", "schemaVersion": "1.0"},
                "messageId": "1",
                "publishTime": "2026-10-19T08:00:00Z",
            },
            "subscription": "projects/your-project-id/subscriptions/budget-alerts-to-poller",
        }
    ).encode()


def test_a_push_holds_the_notification():
    assert parse_push(push(NOTIFICATION)) == NOTIFICATION
    assert parse_push(push(NOTIFICATION, urlsafe=True)) == NOTIFICATION


def test_padding_may_be_missing():
    body = json.loads(push(NOTIFICATION))
    body["message"]["data"] = body["message"]["data"].rstrip("=")
    assert parse_push(json.dumps(body).encode()) == NOTIFICATION


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        json.dumps({"message": {}}).encode(),
        json.dumps({"message": {"data": 5}}).encode(),
        json.dumps({"message": {"data": "!!!"}}).encode(),
        push(b"not json inside"),
        push([1, 2]),
        b" " * (MAX_BODY + 1),
    ],
)
def test_other_pushes_are_refused(body):
    with pytest.raises(BudgetMessageError):
        parse_push(body)


def test_errors_never_echo_the_content():
    secret = "billing-0000-0000"
    with pytest.raises(BudgetMessageError) as info:
        parse_push(push(f"oops {secret}".encode()))
    assert secret not in str(info.value)


def test_a_notification_parses():
    notice = parse_notification(NOTIFICATION)
    assert notice == BudgetNotice(
        cost=3.80, budget=3.75, currency="GBP", threshold=1.0, forecast=None, period_start=utc(2026, 10, 1, 7)
    )
    assert notice.ratio == pytest.approx(3.80 / 3.75)
    assert notice.over


@pytest.mark.parametrize(
    ("fields", "over"),
    [
        ({"costAmount": 3.0, "alertThresholdExceeded": 0.9}, False),
        ({"costAmount": 3.75}, True),  # exactly 100%
        ({"costAmount": 1.0, "alertThresholdExceeded": 1.0}, True),  # the threshold says so
        ({"costAmount": 2.0, "forecastThresholdExceeded": 1.0}, False),  # a forecast alone never closes
        ({"costAmount": 5.0, "budgetAmount": 0}, False),  # no budget amount: no ratio
        ({"costAmount": "3.9", "budgetAmount": "3.75"}, True),  # numbers as strings
    ],
)
def test_only_actual_spend_at_the_budget_is_over(fields, over):
    data = {k: v for k, v in NOTIFICATION.items() if k != "alertThresholdExceeded"} | fields
    assert parse_notification(data).over is over


@pytest.mark.parametrize("missing", ["costAmount", "budgetAmount"])
def test_a_notification_needs_amounts(missing):
    with pytest.raises(BudgetMessageError):
        parse_notification({k: v for k, v in NOTIFICATION.items() if k != missing})


@pytest.mark.parametrize("bad", [True, -1, float("nan"), "lots", None])
def test_odd_amounts_are_refused(bad):
    with pytest.raises(BudgetMessageError):
        parse_notification(NOTIFICATION | {"costAmount": bad})


NOW = utc(2026, 10, 19, 8)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-10-01T07:00:00Z", utc(2026, 11, 1, 7)),  # Pacific time, summer
        ("2026-11-01T07:00:00.000Z", utc(2026, 12, 1, 7)),
        ("2026-10-01T00:00:00Z", utc(2026, 11, 1)),  # UTC
        ("2026-09-30T15:00:00Z", utc(2026, 10, 31, 15)),  # a zone ahead of UTC (+09:00)
        ("2026-10-01T00:00:00-07:00", utc(2026, 11, 1, 7)),
        (None, utc(2026, 11, 1)),  # unknown: the next UTC month
        ("garbage", utc(2026, 11, 1)),
        ("2026-10-15T12:00:00Z", utc(2026, 11, 1)),  # not a month start anywhere
    ],
)
def test_the_period_ends_on_the_next_first(start, end):
    data = dict(NOTIFICATION)
    if start is None:
        del data["costIntervalStart"]
    else:
        data["costIntervalStart"] = start
    assert parse_notification(data).period_end(NOW) == end
