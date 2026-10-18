"""Focused pure-validator tests, independent of the Hermes runtime."""
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "temporal_under_test",
    Path(__file__).resolve().parents[1] / "src/hermes_xmemo/xmemo/temporal.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
validate = module.validate_time_filters


def test_omitted_and_stripped_fields():
    assert validate() == {}
    assert validate(since=None) == {}
    assert validate(owner_timezone=" Asia/Tokyo ", query_mode=" topic ") == {
        "owner_timezone": "Asia/Tokyo", "query_mode": "topic",
    }
    assert validate(owner_timezone="server-validates-this") == {
        "owner_timezone": "server-validates-this",
    }


@pytest.mark.parametrize("instant", [
    "2026-10-01T00:00:00+09:00", "2026-09-30T15:00:00Z",
    "2024-02-29T23:59:59.123456789Z", "2026-10-01t00:00:00z",
    "2026-10-01T00:00:00-07:30",
])
def test_valid_instants_preserved(instant):
    assert validate(since=instant, reference_time=instant) == {
        "since": instant, "reference_time": instant,
    }


@pytest.mark.parametrize("field", ["since", "until", "reference_time"])
@pytest.mark.parametrize("instant", [
    "2026-10-01T00:00:00", "2026-02-30T00:00:00Z", "2025-02-29T00:00:00Z",
    "2026-10-01T24:00:00Z", "2026-10-01T24:00:00.0+09:00",
    "2026-10-01T23:60:00Z", "2026-10-01T23:59:60Z",
    "2026-10-01T00:00:00+24:00",
    "2026-10-01T00:00:00+09:60", "2026-10-01 00:00:00Z",
    "2026-10-01T00:00:00+0900", "0000-01-01T00:00:00Z",
])
def test_invalid_instants(field, instant):
    with pytest.raises(ValueError, match=field):
        validate(**{field: instant})


@pytest.mark.parametrize("field", [
    "since", "until", "reference_time", "owner_timezone", "time_order",
    "query_mode", "temporal_axis_mode", "hypothetical_answer",
])
@pytest.mark.parametrize("value", ["", " \t ", 123, False, [], {}])
def test_bad_types_and_empty(field, value):
    with pytest.raises(ValueError, match=field):
        validate(**{field: value}, allow_hypothetical=True)


@pytest.mark.parametrize("field,values", [
    ("time_order", ["asc", "desc"]),
    ("query_mode", ["topic", "activity_log"]),
    ("temporal_axis_mode", ["created_at", "updated_at", "split_filter_created_order_updated"]),
])
def test_enum_values(field, values):
    for value in values:
        bounds = {"since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00Z"}
        assert validate(**bounds, **{field: value})[field] == value
    with pytest.raises(ValueError, match=field):
        validate(**{field: "INVALID"})


@pytest.mark.parametrize("bounds", [{}, {"since": "2026-10-01T00:00:00Z"},
                                       {"until": "2026-10-02T00:00:00Z"}])
def test_activity_requires_both_bounds(bounds):
    with pytest.raises(ValueError, match="activity_log requires both"):
        validate(query_mode="activity_log", **bounds)
    assert validate(query_mode="topic", **bounds) == {"query_mode": "topic", **bounds}


@pytest.mark.parametrize("until", ["2026-09-30T15:00:00Z", "2026-09-30T14:59:59Z"])
def test_jst_utc_equal_or_reversed_rejected(until):
    with pytest.raises(ValueError, match="strictly later"):
        validate(since="2026-10-01T00:00:00+09:00", until=until)


def test_submicrosecond_ordering_and_offset_rollover():
    assert validate(since="2026-10-01T00:00:00+09:00",
                    until="2026-09-30T15:00:00.000000001Z")
    with pytest.raises(ValueError, match="strictly later"):
        validate(since="2026-09-30T15:00:00.000000002Z",
                 until="2026-10-01T00:00:00.000000001+09:00")


def test_hypothetical_requires_opt_in():
    with pytest.raises(ValueError, match="recall_context"):
        validate(hypothetical_answer="answer")
    assert validate(hypothetical_answer=" answer ", allow_hypothetical=True) == {
        "hypothetical_answer": "answer",
    }
