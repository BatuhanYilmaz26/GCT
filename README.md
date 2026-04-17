# Offline Timezone API

An offline FastAPI service that resolves a location to the current local time using local `pytz` and `pycountry` data only. It does not call external APIs at request time.

This version is tuned for a showcase deployment where reliability, predictable behavior, and observability matter:
- Single-timezone countries resolve directly.
- Common country aliases such as `Turkey`, `Turkiye`, `UK`, and `UAE` are normalized before lookup.
- City names resolve to canonical timezone names.
- Canonical timezone queries such as `Europe/Berlin` and `UTC` are supported.
- Near-miss typos can resolve through fuzzy matching.
- Ambiguous countries such as `Brazil`, and aliases that map to multi-timezone countries, return `409` instead of guessing.
- Non-canonical timezone names such as `US/Pacific` are excluded from the public timezone catalog.

## Features

- Offline timezone lookup with no external API dependency.
- Canonical supported timezone catalog derived from `pytz.country_timezones` plus `UTC`.
- Immutable in-memory lookup tables for stable long-running service behavior.
- Cached timezone resolution and cached `pytz.timezone()` objects for lower repeat-request overhead.
- Startup cache warmup for supported timezones.
- `health` and `ready` endpoints for monitoring and process supervision.
- GZip compression for larger responses such as the timezone catalog.
- Response headers for request tracing and timing: `X-Request-ID`, `X-Response-Time-Ms`, `X-Service`, `X-Service-Version`.
- Environment-driven runtime tuning for workers, backlog, keep-alive, logging, and resolver cache size.
- Included standard-library smoke/load test script with service checks, correctness checks, a 100-request burst test, and mixed concurrency validation.

## Requirements

- Python 3.9+

## Setup

```bash
python -m venv venv

# Windows
.\venv\Scripts\activate

# macOS / Linux
# source venv/bin/activate

pip install -r requirements.txt
```

## Run

Default local run:

```bash
python main.py
```

Direct Uvicorn run:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

For showcase runs, prefer `python main.py` so the environment-based runtime settings and the metadata reported by `/`, `/health`, and `/ready` stay aligned.

Recommended showcase run for burst traffic:

PowerShell:

```powershell
$env:API_WORKERS = "2"
$env:API_BACKLOG = "2048"
$env:API_KEEP_ALIVE_TIMEOUT = "5"
python main.py
```

Interactive docs are available at `http://127.0.0.1:8000/docs`.

## Runtime Settings

- `API_HOST`: bind address, default `127.0.0.1`
- `API_PORT`: bind port, default `8000`
- `API_WORKERS`: number of Uvicorn worker processes, default `1`
- `API_LOG_LEVEL`: Uvicorn log level, default `info`
- `API_ACCESS_LOG`: enable or disable access logs, default `true`
- `API_BACKLOG`: socket backlog, default `2048`
- `API_KEEP_ALIVE_TIMEOUT`: keep-alive timeout in seconds, default `5`
- `RESOLVE_CACHE_SIZE`: LRU cache size for resolved queries, default `4096`

## API

### `GET /`

Basic service metadata.

Sample call:

```bash
curl "http://127.0.0.1:8000/"
```

Example response:

```json
{
  "status": "online",
  "service": "offline-timezone-api",
  "version": "2.1.0",
  "docs_url": "/docs",
  "endpoints": ["/", "/health", "/ready", "/api/v1/time", "/api/v1/timezones"],
  "supported_timezones": 419,
  "configured_workers": 1
}
```

### `GET /health`

Liveness and runtime metadata.

Sample call:

```bash
curl "http://127.0.0.1:8000/health"
```

Example response shape:

```json
{
  "status": "ready",
  "service": "offline-timezone-api",
  "version": "2.1.0",
  "ready": true,
  "uptime_seconds": 12.534,
  "started_at_utc": "2026-04-17T18:07:41+00:00",
  "hostname": "demo-host",
  "supported_timezones": 419,
  "configured_workers": 1,
  "resolve_cache": {
    "hits": 0,
    "misses": 0,
    "maxsize": 4096,
    "currsize": 0
  },
  "timezone_cache": {
    "hits": 0,
    "misses": 419,
    "maxsize": 419,
    "currsize": 419
  }
}
```

### `GET /ready`

Readiness endpoint. Returns `200` when startup warmup is complete. Returns `503` if the process is not ready to serve traffic.

Sample call:

```bash
curl "http://127.0.0.1:8000/ready"
```

Legacy compatibility aliases `/healthz` and `/readyz` are still accepted by the app, but `/health` and `/ready` are the primary public endpoints.

### `GET /api/v1/time?location=<value>`

Resolves a location into the current local time.

Accepted input types:
- A single-timezone country such as `Finland`
- A supported country alias such as `Turkey` or `Turkiye`
- A city name such as `New York`
- A canonical timezone such as `Europe/Berlin`
- A close typo such as `finlan` or `berl`

If an alias resolves to a country with multiple timezones, the API returns `409` instead of choosing one arbitrarily.

Query constraints:
- Minimum length: 2 characters
- Maximum length: 100 characters

Sample API calls:

Browser or chatbot platform GET request:

```text
GET http://127.0.0.1:8000/api/v1/time?location=Finland
```

`curl` example:

```bash
curl "http://127.0.0.1:8000/api/v1/time?location=Finland"
```

City name with URL encoding:

```bash
curl "http://127.0.0.1:8000/api/v1/time?location=New%20York"
```

Canonical timezone example:

```bash
curl "http://127.0.0.1:8000/api/v1/time?location=Europe/Berlin"
```

PowerShell example:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/time?location=Turkey"
```

Chatbot platform example:

```text
GET /api/v1/time?location=Istanbul
```

Expected usage in a chatbot integration:
- The chatbot sends a GET request with the user-provided location.
- The platform reads `resolved_timezone`, `datetime_iso`, `date`, and `time` from the JSON response.
- If the API returns `409`, the chatbot should ask the user for a more specific city or timezone.
- If the API returns `404`, the chatbot should ask the user to rephrase the location.

Successful example:

```json
{
  "location_query": "Finland",
  "match_type": "country",
  "resolved_timezone": "Europe/Helsinki",
  "timezone_abbreviation": "EEST",
  "datetime_iso": "2026-04-17T20:15:42+03:00",
  "date": "2026-04-17",
  "time": "20:15:42",
  "utc_offset": "+03:00"
}
```

Ambiguous example (`409 Conflict`):

```json
{
  "error": "ambiguous_location",
  "detail": "'Brazil' matches Brazil, which spans multiple timezones. Use a city name or an exact timezone such as Europe/Berlin.",
  "candidates": [
    "America/Noronha",
    "America/Belem",
    "America/Fortaleza",
    "America/Recife",
    "America/Araguaina"
  ]
}
```

Not found example (`404 Not Found`):

```json
{
  "error": "location_not_found",
  "detail": "Could not resolve a supported timezone for 'US/Pacific'. Use a city, a single-timezone country, or a canonical timezone such as Europe/Berlin.",
  "candidates": []
}
```

Validation example (`422 Unprocessable Entity`):

- Returned automatically by FastAPI when `location` is missing or exceeds the 100 character limit.

### `GET /api/v1/timezones`

Returns the supported canonical timezone catalog.

Sample call:

```bash
curl "http://127.0.0.1:8000/api/v1/timezones"
```

Example response shape:

```json
{
  "count": 419,
  "timezones": [
    {
      "timezone": "Africa/Abidjan",
      "countries": ["Cote d'Ivoire"]
    },
    {
      "timezone": "Africa/Accra",
      "countries": ["Ghana"]
    }
  ]
}
```

## Response Headers

All normal API responses and handled error responses include:
- `X-Request-ID`: request correlation ID, generated if the client does not send one
- `X-Response-Time-Ms`: server-side request processing time in milliseconds
- `X-Service`: service identifier, currently `offline-timezone-api`
- `X-Service-Version`: application version, currently `2.1.0`

## Resolution Rules

The resolver in [main.py](main.py) applies these steps in order:

1. Exact single-timezone country match
2. Exact city match from canonical timezone suffixes
3. Exact canonical timezone match
4. Fuzzy fallback with strict score cutoffs
5. Explicit error for ambiguous or unsupported inputs

Important behavior:
- Multi-timezone countries are not mapped to the first timezone in `pytz`.
- Common country aliases are normalized before lookup.
- Country aliases and non-canonical timezone aliases are never returned as public timezone values; responses always use canonical timezone names such as `Europe/Istanbul`.
- Short fuzzy matches use stricter cutoffs to reduce incorrect guesses.

## Operational Readiness

This project does not include Google Cloud enterprise services, authentication, authorization, rate limiting, WAF rules, or audit logging.

What it does include:
- Startup cache warmup so workers are ready before traffic is accepted.
- `health` and `ready` endpoints for liveness and readiness checks.
- Multi-worker support through `API_WORKERS`.
- Configurable socket backlog and keep-alive timeout.
- Open CORS for `GET` requests with credentials disabled.
- GZip compression for larger payloads.
- Structured `404`, `409`, and `500` JSON responses.
- A generic `500` payload to avoid leaking internal tracebacks to clients.

Important note on true 24/7 uptime:

Application code helps with readiness and recoverability, but real 24/7 uptime still depends on running the process under a supervisor such as a Windows Service, NSSM, Task Scheduler at startup, `systemd`, or a container restart policy. This repository now exposes the operational endpoints needed for that kind of deployment.

## Testing And Local Validation

Start the API in one terminal:

```bash
python main.py
```

Then run the bundled validation script in another:

```bash
python test_load.py
```

Validation script settings:
- `API_BASE_URL`: target service base URL, default `http://127.0.0.1:8000`
- `BURST_REQUESTS`: request count for the burst test, default `100`, minimum effective value `20`

The script checks:
1. Root, health, and readiness endpoints
2. Exact country, city, timezone, and fuzzy matches
3. A same-second burst test with 100 concurrent requests by default
4. A mixed concurrency scenario including `200`, `404`, and `409` responses
5. Timezone catalog consistency

Latest local validation on this machine using `API_WORKERS=2`:
- 100 concurrent burst requests: `100/100` successful `200` responses
- Burst throughput: about `1146` requests/sec
- Burst latency: about `24.87 ms` p50, `34.96 ms` p95, `38.10 ms` max
- Mixed 160-request concurrency run: about `1442` requests/sec with zero failures

These numbers are local measurements, not a fixed SLA. Use `test_load.py` on the target machine to measure actual performance.

## Limitations

- Countries with multiple timezones require a city name or exact timezone.
- The supported catalog is based on local `pytz` and `pycountry` data.
- Only canonical supported timezone names and `UTC` are returned by the API.
- The project does not implement authentication, rate limiting, or automatic process restarts.
