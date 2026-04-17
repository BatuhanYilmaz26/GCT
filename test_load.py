import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ROOT_ENDPOINT = f"{BASE_URL}/"
HEALTH_ENDPOINT = f"{BASE_URL}/health"
READY_ENDPOINT = f"{BASE_URL}/ready"
TIME_ENDPOINT = f"{BASE_URL}/api/v1/time"
TIMEZONES_ENDPOINT = f"{BASE_URL}/api/v1/timezones"
REQUEST_TIMEOUT_SECONDS = 5
BURST_REQUESTS = max(20, int(os.getenv("BURST_REQUESTS", "100")))
EXPECTED_SUCCESS_KEYS = {
    "location_query",
    "match_type",
    "resolved_timezone",
    "timezone_abbreviation",
    "datetime_iso",
    "date",
    "time",
    "utc_offset",
}

TEST_CASES = [
    {
        "label": "single-timezone country",
        "location": "Finland",
        "expected_status": 200,
        "expected_timezone": "Europe/Helsinki",
        "expected_match_type": "country",
    },
    {
        "label": "city name",
        "location": "New York",
        "expected_status": 200,
        "expected_timezone": "America/New_York",
        "expected_match_type": "city",
    },
    {
        "label": "canonical timezone",
        "location": "Europe/Berlin",
        "expected_status": 200,
        "expected_timezone": "Europe/Berlin",
        "expected_match_type": "timezone",
    },
    {
        "label": "country alias",
        "location": "Turkey",
        "expected_status": 200,
        "expected_timezone": "Europe/Istanbul",
        "expected_match_type": "country",
    },
    {
        "label": "fuzzy country",
        "location": "finlan",
        "expected_status": 200,
        "expected_timezone": "Europe/Helsinki",
        "expected_match_type": "fuzzy_country",
    },
    {
        "label": "fuzzy city",
        "location": "berl",
        "expected_status": 200,
        "expected_timezone": "Europe/Berlin",
        "expected_match_type": "fuzzy_city",
    },
    {
        "label": "ambiguous country",
        "location": "Brazil",
        "expected_status": 409,
        "expected_error": "ambiguous_location",
        "requires_candidates": True,
    },
    {
        "label": "unsupported legacy alias",
        "location": "US/Pacific",
        "expected_status": 404,
        "expected_error": "location_not_found",
    },
    {
        "label": "invalid location",
        "location": "Atlantis",
        "expected_status": 404,
        "expected_error": "location_not_found",
    },
    {
        "label": "oversized payload",
        "location": "A" * 5000,
        "expected_status": 422,
    },
]

CONCURRENCY_LOCATIONS = [
    "Finland",
    "New York",
    "Europe/Berlin",
    "Turkey",
    "finlan",
    "berl",
    "Brazil",
    "Atlantis",
]

BURST_LOCATIONS = ["Finland", "New York", "Europe/Berlin", "Turkey"]


def fetch_json(url: str):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8")
        payload = json.loads(response_body) if response_body else {}
        return exc.code, payload
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach the API at {BASE_URL}. Start the server before running this script."
        ) from exc


def request_time(location: str):
    url = f"{TIME_ENDPOINT}?location={urllib.parse.quote(location)}"
    status, payload = fetch_json(url)
    return location, status, payload


def request_time_with_timing(location: str):
    started_at = time.perf_counter()
    result = request_time(location)
    duration_ms = (time.perf_counter() - started_at) * 1000
    return result[0], result[1], result[2], duration_ms


def percentile(values, ratio: float) -> float:
    if not values:
        return 0.0
    ordered_values = sorted(values)
    index = min(len(ordered_values) - 1, max(0, int(round((len(ordered_values) - 1) * ratio))))
    return ordered_values[index]


def run_service_endpoint_checks() -> int:
    print("--- 1. Service endpoint checks ---")
    failures = 0

    root_status, root_payload = fetch_json(ROOT_ENDPOINT)
    if root_status != 200:
        failures += 1
        print(f"FAIL [{root_status}] root endpoint")
    else:
        required_root_keys = {"status", "service", "version", "docs_url", "endpoints", "supported_timezones", "configured_workers"}
        missing_root_keys = sorted(required_root_keys - root_payload.keys())
        if missing_root_keys:
            failures += 1
            print(f"FAIL root payload missing keys: {missing_root_keys}")
        else:
            print("PASS [200] root endpoint")

    for label, url in (("health", HEALTH_ENDPOINT), ("ready", READY_ENDPOINT)):
        status, payload = fetch_json(url)
        if status != 200:
            failures += 1
            print(f"FAIL [{status}] {label}")
            continue

        required_health_keys = {
            "status",
            "service",
            "version",
            "ready",
            "uptime_seconds",
            "started_at_utc",
            "hostname",
            "supported_timezones",
            "configured_workers",
            "resolve_cache",
            "timezone_cache",
        }
        missing_health_keys = sorted(required_health_keys - payload.keys())
        if missing_health_keys:
            failures += 1
            print(f"FAIL {label} payload missing keys: {missing_health_keys}")
        elif not payload.get("ready"):
            failures += 1
            print(f"FAIL {label} reported ready=false")
        else:
            print(f"PASS [200] {label}")

    return failures


def validate_success_payload(payload: dict, expected_timezone: str, expected_match_type: str) -> str:
    missing_keys = sorted(EXPECTED_SUCCESS_KEYS - payload.keys())
    if missing_keys:
        return f"missing keys: {missing_keys}"

    if payload.get("resolved_timezone") != expected_timezone:
        return f"expected timezone {expected_timezone}, got {payload.get('resolved_timezone')}"

    if payload.get("match_type") != expected_match_type:
        return f"expected match_type {expected_match_type}, got {payload.get('match_type')}"

    if "T" not in payload.get("datetime_iso", ""):
        return "datetime_iso is not an ISO 8601 datetime"

    if len(payload.get("utc_offset", "")) != 6 or payload.get("utc_offset", "")[3] != ":":
        return f"utc_offset is not in +HH:MM format: {payload.get('utc_offset')}"

    return ""


def run_correctness_checks() -> int:
    print("\n--- 2. API correctness checks ---")
    failures = 0

    for case in TEST_CASES:
        location, status, payload = request_time(case["location"])
        error_message = ""

        if status != case["expected_status"]:
            error_message = f"expected status {case['expected_status']}, got {status}"
        elif status == 200:
            error_message = validate_success_payload(
                payload,
                case["expected_timezone"],
                case["expected_match_type"],
            )
        elif "expected_error" in case and payload.get("error") != case["expected_error"]:
            error_message = f"expected error {case['expected_error']}, got {payload.get('error')}"
        elif case.get("requires_candidates") and not payload.get("candidates"):
            error_message = "expected candidate timezones in the error payload"

        if error_message:
            failures += 1
            print(f"FAIL [{status}] {case['label']}: {location} -> {error_message}")
        else:
            print(f"PASS [{status}] {case['label']}: {location}")

    return failures


def run_timezone_catalog_check() -> int:
    print("\n--- 5. Catalog checks ---")
    failures = 0
    status, payload = fetch_json(TIMEZONES_ENDPOINT)

    if status != 200:
        print(f"FAIL [{status}] could not fetch timezone catalog")
        return 1

    timezones = payload.get("timezones", [])
    timezone_names = {entry.get("timezone") for entry in timezones}

    if payload.get("count") != len(timezones):
        failures += 1
        print(f"FAIL catalog count mismatch: count={payload.get('count')} len(timezones)={len(timezones)}")
    else:
        print(f"PASS catalog count: {payload.get('count')}")

    for timezone_name in ("UTC", "Europe/Berlin", "Europe/Istanbul"):
        if timezone_name not in timezone_names:
            failures += 1
            print(f"FAIL missing supported timezone: {timezone_name}")
        else:
            print(f"PASS supported timezone present: {timezone_name}")

    for legacy_alias in ("Turkey", "US/Pacific"):
        if legacy_alias in timezone_names:
            failures += 1
            print(f"FAIL legacy alias should not be listed: {legacy_alias}")
        else:
            print(f"PASS legacy alias excluded: {legacy_alias}")

    return failures


def run_concurrency_smoke_test() -> int:
    print("\n--- 4. Mixed concurrency smoke test ---")
    failures = 0
    payloads = CONCURRENCY_LOCATIONS * 20
    started_at = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as executor:
        results = list(executor.map(request_time, payloads))

    elapsed = time.perf_counter() - started_at
    status_counts = {}
    for _location, status, _payload in results:
        status_counts[status] = status_counts.get(status, 0) + 1

    unexpected_statuses = sorted(status for status in status_counts if status not in {200, 404, 409})
    if unexpected_statuses:
        failures += 1
        print(f"FAIL unexpected statuses under concurrency: {unexpected_statuses}")
    else:
        print(f"PASS status distribution: {status_counts}")

    server_errors = status_counts.get(500, 0)
    if server_errors:
        failures += 1
        print(f"FAIL received {server_errors} internal server errors")

    throughput = len(payloads) / elapsed if elapsed else 0.0
    print(f"Executed {len(payloads)} requests in {elapsed:.4f} seconds")
    print(f"Throughput: {throughput:.2f} requests/sec")

    return failures


def run_burst_test() -> int:
    print(f"\n--- 3. {BURST_REQUESTS}-request burst test ---")
    failures = 0
    payloads = [BURST_LOCATIONS[index % len(BURST_LOCATIONS)] for index in range(BURST_REQUESTS)]
    started_at = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(BURST_REQUESTS, 100)) as executor:
        results = list(executor.map(request_time_with_timing, payloads))

    elapsed = time.perf_counter() - started_at
    durations_ms = [result[3] for result in results]
    non_200_results = [result for result in results if result[1] != 200]

    if non_200_results:
        failures += 1
        grouped_statuses = {}
        for _location, status, _payload, _duration_ms in non_200_results:
            grouped_statuses[status] = grouped_statuses.get(status, 0) + 1
        print(f"FAIL burst status distribution: {grouped_statuses}")
    else:
        print("PASS all burst requests returned 200")

    print(f"Executed {len(payloads)} requests in {elapsed:.4f} seconds")
    print(f"Throughput: {len(payloads) / elapsed:.2f} requests/sec")
    print(f"p50 latency: {percentile(durations_ms, 0.50):.2f} ms")
    print(f"p95 latency: {percentile(durations_ms, 0.95):.2f} ms")
    print(f"max latency: {max(durations_ms) if durations_ms else 0.0:.2f} ms")

    return failures


def main() -> int:
    try:
        failures = 0
        failures += run_service_endpoint_checks()
        failures += run_correctness_checks()
        failures += run_burst_test()
        failures += run_concurrency_smoke_test()
        failures += run_timezone_catalog_check()
    except RuntimeError as exc:
        print(f"FAIL {exc}")
        return 1

    print(f"\nTotal failures: {failures}")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
