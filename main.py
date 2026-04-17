import os
import logging
import secrets
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from functools import lru_cache
from time import perf_counter
from types import MappingProxyType
from typing import Dict, List, Optional, Tuple

import pycountry
from collections import defaultdict
import pytz
from fastapi import FastAPI, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

SERVICE_NAME = "offline-timezone-api"
APP_VERSION = "2.1.0"

FUZZY_SCORE_CUTOFF = 88
FUZZY_SCORE_CUTOFF_SHORT_QUERY = 92
MAX_AMBIGUOUS_SUGGESTIONS = 5


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    workers: int
    log_level: str
    access_log: bool
    backlog: int
    timeout_keep_alive: int


def read_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().casefold() in {"1", "true", "yes", "on"}


def read_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        return max(minimum, int(raw_value))
    except ValueError:
        logger.warning("Invalid integer for %s: %s. Falling back to %s.", name, raw_value, default)
        return default


SETTINGS = ServerSettings(
    host=os.getenv("API_HOST", "127.0.0.1"),
    port=read_int_env("API_PORT", 8000),
    workers=read_int_env("API_WORKERS", 1),
    log_level=os.getenv("API_LOG_LEVEL", "info").strip().lower() or "info",
    access_log=read_bool_env("API_ACCESS_LOG", True),
    backlog=read_int_env("API_BACKLOG", 2048),
    timeout_keep_alive=read_int_env("API_KEEP_ALIVE_TIMEOUT", 5),
)
RESOLVE_CACHE_SIZE = read_int_env("RESOLVE_CACHE_SIZE", 4096)


def normalize_location(value: str) -> str:
    normalized = (
        value.strip()
        .casefold()
        .replace("_", " ")
        .replace("-", " ")
        .replace("&", " and ")
        .replace("'", "")
        .replace('"', "")
        .replace(",", " ")
        .replace(".", "")
        .replace("(", " ")
        .replace(")", " ")
    )
    return " ".join(normalized.split())


def normalize_timezone_query(value: str) -> str:
    return value.strip().casefold().replace(" ", "_")


def format_utc_offset(value: str) -> str:
    if len(value) != 5:
        return value
    return f"{value[:3]}:{value[3:]}"


def fuzzy_score_cutoff(query: str) -> int:
    if len(query) < 4:
        return FUZZY_SCORE_CUTOFF_SHORT_QUERY
    if len(query) < 6:
        return 90
    return FUZZY_SCORE_CUTOFF


@dataclass(frozen=True)
class ResolutionResult:
    timezone: str
    match_type: str


class LocationResolutionError(Exception):
    status_code = 404
    error = "location_not_found"

    def __init__(self, detail: str, candidates: Optional[List[str]] = None):
        super().__init__(detail)
        self.detail = detail
        self.candidates = candidates or []


class AmbiguousLocationError(LocationResolutionError):
    status_code = 409
    error = "ambiguous_location"


class ErrorResponse(BaseModel):
    error: str
    detail: str
    candidates: List[str] = Field(default_factory=list)


class CacheStatsResponse(BaseModel):
    hits: int
    misses: int
    maxsize: Optional[int]
    currsize: int


class TimeResponse(BaseModel):
    location_query: str
    match_type: str
    resolved_timezone: str
    timezone_abbreviation: str
    datetime_iso: str
    date: str
    time: str
    utc_offset: str


class RootResponse(BaseModel):
    status: str
    service: str
    version: str
    docs_url: str
    endpoints: List[str]
    supported_timezones: int
    configured_workers: int


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    ready: bool
    uptime_seconds: float
    started_at_utc: str
    hostname: str
    supported_timezones: int
    configured_workers: int
    resolve_cache: CacheStatsResponse
    timezone_cache: CacheStatsResponse


def build_cache_stats(cache_info) -> CacheStatsResponse:
    return CacheStatsResponse(
        hits=cache_info.hits,
        misses=cache_info.misses,
        maxsize=cache_info.maxsize,
        currsize=cache_info.currsize,
    )


def get_started_at_utc(request_app: FastAPI) -> datetime:
    started_at_utc = getattr(request_app.state, "started_at_utc", None)
    if started_at_utc is None:
        return datetime.now(dt_timezone.utc)
    return started_at_utc


def get_uptime_seconds(request_app: FastAPI) -> float:
    started_at_perf = getattr(request_app.state, "started_at_perf", None)
    if started_at_perf is None:
        return 0.0
    return round(max(0.0, perf_counter() - started_at_perf), 3)


def build_health_response(request_app: FastAPI) -> HealthResponse:
    started_at_utc = get_started_at_utc(request_app)
    ready = bool(getattr(request_app.state, "ready", False))

    return HealthResponse(
        status="ready" if ready else "starting",
        service=SERVICE_NAME,
        version=APP_VERSION,
        ready=ready,
        uptime_seconds=get_uptime_seconds(request_app),
        started_at_utc=started_at_utc.isoformat(timespec="seconds"),
        hostname=socket.gethostname(),
        supported_timezones=len(cached_timezone_list),
        configured_workers=SETTINGS.workers,
        resolve_cache=build_cache_stats(resolve_timezone.cache_info()),
        timezone_cache=build_cache_stats(get_timezone_object.cache_info()),
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.started_at_utc = datetime.now(dt_timezone.utc)
    application.state.started_at_perf = perf_counter()
    application.state.instance_id = secrets.token_hex(8)
    application.state.ready = False

    warm_timezone_cache()
    application.state.ready = True
    logger.info(
        "Service ready: supported_timezones=%s workers=%s backlog=%s keep_alive=%ss",
        len(cached_timezone_list),
        SETTINGS.workers,
        SETTINGS.backlog,
        SETTINGS.timeout_keep_alive,
    )

    try:
        yield
    finally:
        application.state.ready = False


app = FastAPI(
    title="Offline Timezone API",
    description="Resolve a city, country, or canonical timezone name to the current local time without calling external APIs.",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=500)

COMMON_COUNTRY_ALIASES = {
    "turkey": "TR",
    "turkiye": "TR",
    "uk": "GB",
    "us": "US",
    "usa": "US",
    "uae": "AE",
    "russia": "RU",
    "south korea": "KR",
    "north korea": "KP",
}

country_name_to_code: Dict[str, str] = {}
country_code_to_name: Dict[str, str] = {}
for country in pycountry.countries:
    country_code_to_name[country.alpha_2] = country.name
    country_name_to_code[normalize_location(country.name)] = country.alpha_2

    official_name = getattr(country, "official_name", None)
    if official_name:
        country_name_to_code[normalize_location(official_name)] = country.alpha_2

    common_name = getattr(country, "common_name", None)
    if common_name:
        country_name_to_code[normalize_location(common_name)] = country.alpha_2

for alias, country_code in COMMON_COUNTRY_ALIASES.items():
    country_name_to_code[normalize_location(alias)] = country_code

country_name_to_code = MappingProxyType(country_name_to_code)
country_code_to_name = MappingProxyType(country_code_to_name)

country_code_to_timezones: Dict[str, Tuple[str, ...]] = {
    country_code: tuple(timezones)
    for country_code, timezones in pytz.country_timezones.items()
}
country_code_to_timezones = MappingProxyType(country_code_to_timezones)

supported_timezones = tuple(
    sorted({tz for timezones in country_code_to_timezones.values() for tz in timezones} | {"UTC"})
)
supported_timezone_lookup = {
    normalize_timezone_query(timezone): timezone for timezone in supported_timezones
}
supported_timezone_lookup = MappingProxyType(supported_timezone_lookup)

timezone_to_countries_map = defaultdict(list)
locale_to_timezones = defaultdict(list)
for country_code, timezones in country_code_to_timezones.items():
    country_name = country_code_to_name.get(country_code, country_code)
    for tz in timezones:
        timezone_to_countries_map[tz].append(country_name)
        locale = normalize_location(tz.rsplit("/", 1)[-1])
        locale_to_timezones[locale].append(tz)

locale_to_timezone = {}
ambiguous_locale_to_timezones = {}
for locale, timezones in locale_to_timezones.items():
    unique_timezones = tuple(sorted(set(timezones)))
    if len(unique_timezones) == 1:
        locale_to_timezone[locale] = unique_timezones[0]
    else:
        ambiguous_locale_to_timezones[locale] = unique_timezones

locale_to_timezone = MappingProxyType(locale_to_timezone)
ambiguous_locale_to_timezones = MappingProxyType(ambiguous_locale_to_timezones)

cached_timezone_list = []
for tz in supported_timezones:
    cached_timezone_list.append(
        {
            "timezone": tz,
            "countries": sorted(timezone_to_countries_map.get(tz, [])),
        }
    )

fuzzy_candidate_lookup = {}
for country_name, country_code in country_name_to_code.items():
    fuzzy_candidate_lookup[country_name] = ("country", country_code)
for locale, timezone in locale_to_timezone.items():
    fuzzy_candidate_lookup.setdefault(locale, ("city", timezone))
for timezone_query, timezone in supported_timezone_lookup.items():
    fuzzy_candidate_lookup.setdefault(timezone_query, ("timezone", timezone))

fuzzy_candidate_lookup = MappingProxyType(fuzzy_candidate_lookup)

all_known_locations = tuple(sorted(fuzzy_candidate_lookup))
all_known_timezones = tuple(sorted(supported_timezone_lookup))


@lru_cache(maxsize=len(supported_timezones))
def get_timezone_object(timezone_name: str):
    return pytz.timezone(timezone_name)


def warm_timezone_cache() -> None:
    for timezone_name in supported_timezones:
        get_timezone_object(timezone_name)


@app.middleware("http")
async def add_response_headers(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)
    request.state.request_id = request_id
    started_at = perf_counter()
    response = await call_next(request)
    duration_ms = (perf_counter() - started_at) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.2f}"
    response.headers["X-Service"] = SERVICE_NAME
    response.headers["X-Service-Version"] = APP_VERSION
    return response


def resolve_country_timezone(country_code: str, location_query: str, match_type: str) -> ResolutionResult:
    timezones = country_code_to_timezones.get(country_code, ())
    if not timezones:
        raise LocationResolutionError(f"No supported timezone data is available for '{location_query}'.")

    if len(timezones) > 1:
        country_name = country_code_to_name.get(country_code, location_query)
        candidates = list(timezones[:MAX_AMBIGUOUS_SUGGESTIONS])
        raise AmbiguousLocationError(
            detail=(
                f"'{location_query}' matches {country_name}, which spans multiple timezones. "
                "Use a city name or an exact timezone such as Europe/Berlin."
            ),
            candidates=candidates,
        )

    return ResolutionResult(timezone=timezones[0], match_type=match_type)


@app.exception_handler(LocationResolutionError)
async def handle_lookup_error(_request: Request, exc: LocationResolutionError):
    payload = ErrorResponse(
        error=exc.error,
        detail=exc.detail,
        candidates=exc.candidates,
    )
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception("Unexpected error while handling %s", request.url.path, exc_info=exc)
    payload = ErrorResponse(
        error="internal_server_error",
        detail="An unexpected error occurred while resolving the location.",
    )
    return JSONResponse(status_code=500, content=payload.model_dump())


@lru_cache(maxsize=RESOLVE_CACHE_SIZE)
def resolve_timezone(location_query: str) -> ResolutionResult:
    """
    Resolve a user-supplied location into a supported canonical timezone.
    """
    normalized_location_query = normalize_location(location_query)
    normalized_timezone_query = normalize_timezone_query(location_query)

    if normalized_location_query in country_name_to_code:
        country_code = country_name_to_code[normalized_location_query]
        return resolve_country_timezone(country_code, location_query, "country")

    if normalized_location_query in locale_to_timezone:
        return ResolutionResult(
            timezone=locale_to_timezone[normalized_location_query],
            match_type="city",
        )

    if normalized_location_query in ambiguous_locale_to_timezones:
        raise AmbiguousLocationError(
            detail=(
                f"'{location_query}' matches multiple locations. "
                "Use a more specific city or an exact timezone name."
            ),
            candidates=list(ambiguous_locale_to_timezones[normalized_location_query][:MAX_AMBIGUOUS_SUGGESTIONS]),
        )

    if normalized_timezone_query in supported_timezone_lookup:
        return ResolutionResult(
            timezone=supported_timezone_lookup[normalized_timezone_query],
            match_type="timezone",
        )

    if len(normalized_location_query) >= 3:
        fuzzy_query = normalized_timezone_query if "/" in normalized_timezone_query else normalized_location_query
        choices = all_known_timezones if "/" in normalized_timezone_query else all_known_locations
        result = process.extractOne(
            fuzzy_query,
            choices,
            scorer=fuzz.WRatio,
            score_cutoff=fuzzy_score_cutoff(fuzzy_query),
        )
        if result is not None:
            match = result[0]
            match_type, target = fuzzy_candidate_lookup[match]

            if match_type == "country":
                return resolve_country_timezone(target, location_query, "fuzzy_country")

            if match_type == "city":
                return ResolutionResult(timezone=target, match_type="fuzzy_city")

            return ResolutionResult(timezone=target, match_type="fuzzy_timezone")

    raise LocationResolutionError(
        f"Could not resolve a supported timezone for '{location_query}'. "
        "Use a city, a single-timezone country, or a canonical timezone such as Europe/Berlin."
    )


@app.get(
    "/api/v1/time",
    response_model=TimeResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_time(
    location: str = Query(
        ...,
        min_length=2,
        max_length=100,
        description="City name, single-timezone country, or canonical timezone such as Europe/Berlin",
    )
):
    """
    Returns the current local time for a given location (city or country).
    """
    result = resolve_timezone(location)
    tz = get_timezone_object(result.timezone)
    now = datetime.now(tz)

    return TimeResponse(
        location_query=location,
        match_type=result.match_type,
        resolved_timezone=result.timezone,
        timezone_abbreviation=now.tzname() or result.timezone,
        datetime_iso=now.isoformat(timespec="seconds"),
        date=now.strftime("%Y-%m-%d"),
        time=now.strftime("%H:%M:%S"),
        utc_offset=format_utc_offset(now.strftime("%z")),
    )


@app.get("/", response_model=RootResponse)
async def root():
    """
    Root endpoint for health check and user-friendly redirect message.
    """
    return RootResponse(
        status="online",
        service=SERVICE_NAME,
        version=APP_VERSION,
        docs_url="/docs",
        endpoints=["/", "/health", "/ready", "/api/v1/time", "/api/v1/timezones"],
        supported_timezones=len(cached_timezone_list),
        configured_workers=SETTINGS.workers,
    )


@app.get("/health", response_model=HealthResponse)
@app.get("/healthz", response_model=HealthResponse, include_in_schema=False)
async def health(request: Request):
    return build_health_response(request.app)


@app.get(
    "/ready",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse}},
)
@app.get(
    "/readyz",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse}},
    include_in_schema=False,
)
async def ready(request: Request):
    payload = build_health_response(request.app)
    if payload.ready:
        return payload

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload.model_dump(),
    )


class TimezoneInfo(BaseModel):
    timezone: str
    countries: List[str]


class TimezoneListResponse(BaseModel):
    count: int
    timezones: List[TimezoneInfo]


TIMEZONE_LIST_RESPONSE = TimezoneListResponse(
    count=len(cached_timezone_list),
    timezones=cached_timezone_list,
)


@app.get("/api/v1/timezones", response_model=TimezoneListResponse)
async def list_timezones():
    """
    Returns the supported canonical timezone catalog and their associated countries.
    """
    return TIMEZONE_LIST_RESPONSE

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=getattr(logging, SETTINGS.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "main:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        log_level=SETTINGS.log_level,
        access_log=SETTINGS.access_log,
        workers=SETTINGS.workers,
        backlog=SETTINGS.backlog,
        timeout_keep_alive=SETTINGS.timeout_keep_alive,
    )
