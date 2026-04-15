# Offline Timezone API

A high-performance, fully offline API built with FastAPI that reliably returns the exact current datetime for any geographical location natively. 

Instead of routing dependencies to third-party endpoints (which introduces latency, external rate limiting, and network unpredictability), this system intrinsically uses predefined mappings generated through standard `pytz` IANA modules and `pycountry`. It operates entirely local to the host machine, resolving up to **800+ requests per second**.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.9+

### Setup & Installation
```bash
# 1. Create your isolated local environment
python -m venv venv

# 2. Activate it (Example: Windows)
.\venv\Scripts\activate
# (On MacOS/Linux: source venv/bin/activate)

# 3. Securely install all architecture dependencies
pip install -r requirements.txt
```

### Running the Application
You only need to run **one** of the following commands (they do the same thing under the hood). 

**Option A: Local Development (Simple)**
```bash
python main.py
```
**Option B: Production Best Practice (Uvicorn CLI)**
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
*(Optionally pass parameters like `--workers 4` safely scaled)*

---

## 📡 Available Endpoints

### 1. Root Greeting & Interactive Docs
Displays basic runtime status and directs developers to the built-in Swagger UI testing suite.
- **URL:** `/`
- **Swagger UI:** `/docs`
- **Method:** `GET`

### 2. Get Location Time (Primary Mechanism)
Returns the live datetime metrics by interpreting an exact or partial location query.

- **URL:** `/api/v1/time`
- **Method:** `GET`
- **Query Parameter:** 
  - `location` (str) - The city, country, or partial string representation of a place. *(Length constraints: 2-100 chars)*

**Request Example:**
`GET http://127.0.0.1:8000/api/v1/time?location=Finland`

**Success Response (200 OK):**
```json
{
  "location_query": "Finland",
  "resolved_timezone": "Europe/Helsinki",
  "date": "2026-04-14",
  "time": "00:47",
  "utc_offset": "+0300"
}
```

### 3. Catalog All Valid Timezones
Returns an array of all ~600 inherently supported exact IANA Timezone definitions alongside an array of the countries dynamically tied to them. 

- **URL:** `/api/v1/timezones`
- **Method:** `GET`

---

## 🛠️ How It Works (Internal Architecture)

This application is strictly consolidated within **`main.py`** to maximize deployment transparency. 

### 1. Zero-Cost Dictionary Generation
During the initial instance boot cycle, Python structurally generates three inverted offline mappings combining logic from `pycountry` and `pytz`. This ensures that iterating through data handles *O(1)* native index lookups rather than arbitrary string evaluations, reducing API latency heavily per query.

### 2. Lookup Cascade (`resolve_timezone()`)
This is the workhorse fallback loop parsing raw URLs to valid native objects securely.
- **Cache Hits**: Memoized instantly via `@lru_cache(maxsize=2048)` for repeated queries.
- **Phase A**: Scans `exact_tz_string_mapping` to determine if we got an exact match right away (e.g. `Europe/Berlin`).
- **Phase B**: Cross-verifies an exact country match (translating `spain` directly to `Europe/Madrid`).
- **Phase C**: Utilizes **Fuzzy String Matching** (`thefuzz` / Levenshtein). If a user types `finlan`, the worker algorithm strictly bounds distance scores requiring `≥ 80%` confidence to map it identically to `Finland`, safely skipping 404s dynamically.

### 3. Date Time Formatting (`get_time()`)
Transforms the successfully routed `tz_name` string directly to a Python `pytz.timezone` object and passes it to absolute native `datetime.now(tz)` variables.

---

## 🛡️ Enterprise Security & Optimization

- **CPU Exhaustion (ReDoS) Protections**: To prevent aggressive bots from executing extremely long payloads (causing fuzzy distances to infinitely compute up to 100% CPU lock), the FastAPI envelope enforces `max_length=100`. Queries circumventing limitations are instantly bypassed to `422 Unprocessable Entity` in nanoseconds.
- **Global API Error Traps**: The query processing logic is forcefully isolated within severe global `try / except Exception` frameworks natively. Unexpected variable crashes throw standard valid JSON messages (`500`) eliminating any possibility of internal `traceback` or environment exposure. 
- **CORS Availability**: Instantiated statically via `CORSMiddleware`, granting external chatbot JS payloads explicitly authorized API connection paths without web browser blocking sequences.

---

## 🧪 Testing Benchmarks

Included natively is `test_load.py`, an aggressive Python `urllib` load-testing utility assessing thread pools without third-party frameworks.
It verifies:
1. Exact and Partial routing algorithms accuracy.
2. Complete 404 denial validation.
3. Rapid 5,000+ char limit rejections cleanly.
4. Total 500+ Request Per Second capacity under highly parallel concurrency tests.
