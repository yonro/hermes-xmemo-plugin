"""Opt-in read-only temporal smoke: XMEMO_LIVE_SMOKE=1 python this_file.py.

Not collected by default pytest discovery. Never prints credentials, IDs,
memory content, response bodies, or exception messages.
"""
from datetime import datetime, timedelta, timezone
import importlib
import logging
import os
from pathlib import Path
import sys
import types


def main():
    if os.environ.get("XMEMO_LIVE_SMOKE") != "1":
        print("SKIP live smoke disabled; set XMEMO_LIVE_SMOKE=1 to opt in")
        return 0

    import httpx

    logging.disable(logging.CRITICAL)
    # Load standalone modules without initializing a Hermes plugin/runtime.
    package = types.ModuleType("_xmemo_live_smoke")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "src/hermes_xmemo/xmemo")]
    sys.modules[package.__name__] = package
    try:
        import hermes_constants  # noqa: F401
    except ModuleNotFoundError:
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        sys.modules["hermes_constants"] = constants
    client_module = importlib.import_module(package.__name__ + ".client")
    config = importlib.import_module(package.__name__ + ".config").load_config(create_instance=False)
    if not config.get("api_key"):
        print("UNVERIFIED credentials unavailable through existing config loader")
        return 2

    class CountingReadTransport(httpx.BaseTransport):
        def __init__(self):
            self.count = 0
            self.inner = httpx.HTTPTransport()

        def handle_request(self, request):
            if (request.method, request.url.path) not in {
                ("GET", "/v1/memories/search"), ("POST", "/v1/recall/context"),
            }:
                raise RuntimeError("Non-read endpoint blocked")
            self.count += 1
            return self.inner.handle_request(request)

        def close(self):
            self.inner.close()

    transport = CountingReadTransport()
    client = client_module.XMemoClient(
        config["base_url"], config["api_key"],
        agent_id=config.get("agent_id", "hermes"),
        agent_instance_id=config.get("agent_instance_id", ""),
        timeout=max(float(config.get("timeout_seconds", 5)), 20), transport=transport,
    )
    jst = timezone(timedelta(hours=9))
    until = datetime.now(jst).replace(hour=0, minute=0, second=0, microsecond=0)
    since = until - timedelta(days=7)
    bounds = {"since": since.isoformat(), "until": until.isoformat()}
    utc_bounds = {name: value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                  for name, value in (("since", since), ("until", until))}
    print(f"WINDOW since={bounds['since']} until={bounds['until']} python={sys.version.split()[0]}")
    outcomes = []

    def report(status, check, detail):
        outcomes.append(status)
        print(f"{status} {check} {detail}")

    def call(method, filters):
        common = {"query": "*", "bucket": config.get("read_bucket", "%"),
                  "scope": config.get("read_scope"), **filters}
        if method == "search":
            return client.search(**common, limit=10)
        return client.recall_context(**common, max_items=10, max_tokens=900)

    def items(result):
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            return result["items"]
        return None

    def timestamp_range(rows, check):
        if not rows:
            report("UNVERIFIED", check, "no returned items to verify")
            return
        checked, missing, invalid, outside = 0, 0, 0, 0
        for row in rows:
            value = (row.get("effective_timestamp") or row.get("created_at")) if isinstance(row, dict) else None
            if not value:
                missing += 1
                continue
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("naive")
                checked += 1
                outside += int(not (since <= stamp < until))
            except (ValueError, TypeError, AttributeError):
                invalid += 1
        status = "FAIL" if outside or invalid else "UNVERIFIED" if missing else "PASS"
        report(status, check, f"checked={checked} missing={missing} invalid={invalid} outside={outside}")

    def ids(rows):
        if not rows or any(not isinstance(row, dict) or not (row.get("id") or row.get("memory_id")) for row in rows):
            return None
        return {str(row.get("id") or row.get("memory_id")) for row in rows}

    try:
        for method in ("search", "recall_context"):
            for axis in ("created_at", "updated_at", "split_filter_created_order_updated"):
                try:
                    result = call(method, {**bounds, "temporal_axis_mode": axis, "query_mode": "topic", "time_order": "desc"})
                    rows = items(result)
                    report("PASS" if rows is not None else "UNVERIFIED", f"{method}.{axis}", f"returned_count={len(rows) if rows is not None else 0}")
                except Exception as exc:
                    report("FAIL", f"{method}.{axis}", f"http_status={getattr(exc, 'status_code', 0)}")
            activity = {"query_mode": "activity_log", "temporal_axis_mode": "created_at", "time_order": "asc"}
            try:
                rows_jst = items(call(method, {**bounds, **activity}))
                report("PASS", f"{method}.activity_log", f"returned_count={len(rows_jst) if rows_jst is not None else 0}")
                timestamp_range(rows_jst, f"{method}.activity_log.range")
                rows_utc = items(call(method, {**utc_bounds, **activity}))
                jst_ids, utc_ids = ids(rows_jst), ids(rows_utc)
                if jst_ids is None or utc_ids is None:
                    report("UNVERIFIED", f"{method}.offset_equivalence", "empty results or missing IDs")
                else:
                    equal = jst_ids == utc_ids
                    report("PASS" if equal else "FAIL", f"{method}.offset_equivalence", f"same_ids={equal} jst_count={len(jst_ids)} utc_count={len(utc_ids)}")
            except Exception as exc:
                report("FAIL", f"{method}.activity_log", f"http_status={getattr(exc, 'status_code', 0)}")

            for name, invalid in (
                ("missing_since", {"query_mode": "activity_log", "until": bounds["until"]}),
                ("missing_until", {"query_mode": "activity_log", "since": bounds["since"]}),
                ("naive_since", {**bounds, "since": since.replace(tzinfo=None).isoformat()}),
                ("invalid_axis", {**bounds, "temporal_axis_mode": "invalid"}),
            ):
                before = transport.count
                rejected = False
                try:
                    call(method, invalid)
                except ValueError:
                    rejected = True
                except Exception:
                    pass
                sent = transport.count - before
                report("PASS" if rejected and sent == 0 else "FAIL", f"{method}.{name}", f"local_ValueError={rejected} actual_requests={sent}")
    finally:
        client.close()
    print(f"SUMMARY pass={outcomes.count('PASS')} fail={outcomes.count('FAIL')} unverified={outcomes.count('UNVERIFIED')} requests={transport.count}")
    return 1 if "FAIL" in outcomes else 2 if "UNVERIFIED" in outcomes else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("FAIL smoke setup/runtime error; details suppressed to protect credentials and memory content")
        raise SystemExit(1)
