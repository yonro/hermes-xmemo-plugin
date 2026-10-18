"""Pure pre-flight validation for optional XMemo retrieval filters."""

from datetime import datetime
from decimal import Decimal, localcontext
import re


_INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_ENUMS = {
    "time_order": {"asc", "desc"},
    "temporal_axis_mode": {
        "created_at", "updated_at", "split_filter_created_order_updated",
    },
    "query_mode": {"topic", "activity_log"},
}


def _instant(value: str, field: str) -> Decimal:
    """Compare exact fractional seconds, without float or microsecond truncation."""
    if not _INSTANT.fullmatch(value):
        raise ValueError(f"{field} must be an RFC 3339 timestamp with timezone offset")
    offset = value[-6:] if value[-1] not in "Zz" else "+00:00"
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    if hours > 23 or minutes > 59:
        raise ValueError(f"{field} has an invalid timezone offset")
    hour, minute, second = int(value[11:13]), int(value[14:16]), int(value[17:19])
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"{field} has an invalid calendar date or time")
    try:
        local = datetime(int(value[:4]), int(value[5:7]), int(value[8:10]),
                         hour, minute, second)
    except ValueError as exc:
        raise ValueError(f"{field} has an invalid calendar date or time") from exc
    fraction_end = len(value) - (1 if value[-1] in "Zz" else 6)
    fraction = value[19:fraction_end]
    seconds = local.toordinal() * 86400 + local.hour * 3600 + local.minute * 60 + local.second
    offset_seconds = (hours * 60 + minutes) * 60
    seconds -= offset_seconds if offset[0] == "+" else -offset_seconds
    # Preserve every supplied fractional digit during decimal arithmetic.
    with localcontext() as context:
        context.prec = max(32, len(fraction) + 16)
        return Decimal(seconds) + Decimal("0" + fraction)


def validate_time_filters(
    *, since=None, until=None, time_order=None, owner_timezone=None,
    reference_time=None, temporal_axis_mode=None, query_mode=None,
    hypothetical_answer=None, allow_hypothetical=False,
) -> dict[str, str]:
    """Return only supplied, stripped fields; reject invalid values with ValueError.

    None denotes an omitted field. Timezone names are validated by the server;
    this helper deliberately requires no timezone database or external I/O.
    """
    fields = {
        "since": since, "until": until, "time_order": time_order,
        "owner_timezone": owner_timezone, "reference_time": reference_time,
        "temporal_axis_mode": temporal_axis_mode, "query_mode": query_mode,
        "hypothetical_answer": hypothetical_answer,
    }
    result = {}
    for name, value in fields.items():
        if value is None:
            continue
        if name == "hypothetical_answer" and not allow_hypothetical:
            raise ValueError("hypothetical_answer is only supported for recall_context")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        result[name] = value.strip()
        if name in _ENUMS and result[name] not in _ENUMS[name]:
            raise ValueError(f"{name} must be one of {', '.join(sorted(_ENUMS[name]))}")
    instants = {name: _instant(result[name], name)
                for name in ("since", "until", "reference_time") if name in result}
    if "since" in instants and "until" in instants and instants["until"] <= instants["since"]:
        raise ValueError("until must be strictly later than since")
    if result.get("query_mode") == "activity_log" and not {"since", "until"} <= result.keys():
        raise ValueError("activity_log requires both since and until")
    return result


def build_cache_params(params: dict, **filters) -> dict:
    """Copy legacy cache params and add only supplied, validated filters.

    The existing cache owns canonical key serialization. Preserve timestamp
    strings (including their offsets) exactly as returned by the validator.
    """
    return {**params, **validate_time_filters(**filters)}
