from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from functools import lru_cache
import pytz
import pycountry
from thefuzz import process
from collections import defaultdict
from typing import List

app = FastAPI(title="Offline Timezone API", description="Provides current time natively without external APIs.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Initialization mappings
# Map lower cased country names to their alpha-2 code
country_name_to_code = {
    "turkey": "TR",
    "uk": "GB",
    "usa": "US",
    "uae": "AE",
    "russia": "RU",
    "korea": "KR",
    "south korea": "KR"
}
for country in pycountry.countries:
    country_name_to_code[country.name.lower()] = country.alpha_2
    if hasattr(country, 'official_name') and country.official_name:
        country_name_to_code[country.official_name.lower()] = country.alpha_2
    if hasattr(country, 'common_name') and country.common_name:
        country_name_to_code[country.common_name.lower()] = country.alpha_2

# Extract city/locale names from timezone names
# "America/New_York" -> "new york"
tz_locale_to_tz_name = {}
exact_tz_string_mapping = {}
for tz in pytz.all_timezones:
    exact_tz_string_mapping[tz.lower()] = tz
    parts = tz.split("/")
    if len(parts) > 1:
        # e.g., "New_York" -> "new york"
        locale = parts[-1].replace("_", " ").lower()
        tz_locale_to_tz_name[locale] = tz

# Build the structural catalog of all timezones for the endpoint mapping
timezone_to_countries_map = defaultdict(list)
for country_code, timezones in pytz.country_timezones.items():
    country_obj = pycountry.countries.get(alpha_2=country_code)
    country_name = country_obj.name if country_obj else country_code
    for tz in timezones:
        timezone_to_countries_map[tz].append(country_name)
        
cached_timezone_list = []
for tz in pytz.all_timezones:
    cached_timezone_list.append({
        "timezone": tz,
        "countries": timezone_to_countries_map.get(tz, [])
    })

# Pre-compute known valid strings to fuzzy match against
all_known_locations = list(country_name_to_code.keys()) + list(tz_locale_to_tz_name.keys())


@lru_cache(maxsize=2048)
def resolve_timezone(location_query: str) -> str:
    """
    Attempts to resolve a user string 'location_query' into an IANA timezone string.
    Returns the valid IANA timezone string or raises ValueError if not found.
    Wrapped in lru_cache for O(1) instantaneous lookups on repetitive requests.
    """
    query = location_query.lower().strip()
    
    # 0. Exact Timezone String Match (e.g. "Europe/Berlin")
    if query in exact_tz_string_mapping:
         return exact_tz_string_mapping[query]
         
    # 1. Exact Country Match
    if query in country_name_to_code:
        country_code = country_name_to_code[query]
        timezones = pytz.country_timezones.get(country_code)
        if timezones:
             return timezones[0] # Return the primary (usually capital) one
             
    # 2. Exact City/Locale Match
    if query in tz_locale_to_tz_name:
         return tz_locale_to_tz_name[query]

    # 3. Fuzzy match (if exact string failed due to typo or slight variation)
    # process.extractOne returns (matched_string, score)
    result = process.extractOne(query, all_known_locations)
    if result:
        match, score = result[0], result[1]
        # Setting a threshold to avoid returning wildly incorrect results for garbage input
        if score >= 80:
            if match in country_name_to_code:
                country_code = country_name_to_code[match]
                timezones = pytz.country_timezones.get(country_code)
                if timezones:
                    return timezones[0]
            elif match in tz_locale_to_tz_name:
                return tz_locale_to_tz_name[match]
              
    raise ValueError(f"Could not resolve a reliable time zone for location '{location_query}'")


class TimeResponse(BaseModel):
    location_query: str
    resolved_timezone: str
    date: str
    time: str
    utc_offset: str


@app.get("/api/v1/time", response_model=TimeResponse)
async def get_time(
    location: str = Query(..., min_length=2, max_length=100, description="The location name to resolve")
):
    """
    Returns the current local time for a given location (city or country).
    """
    try:
        tz_name = resolve_timezone(location)
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        
        return TimeResponse(
            location_query=location,
            resolved_timezone=tz_name,
            date=now.strftime('%Y-%m-%d'),
            time=now.strftime('%H:%M'),
            utc_offset=now.strftime('%z')
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Fallback to prevent raw traceback leakage
        raise HTTPException(status_code=500, detail="An unexpected error occurred resolving the timestamp.")


@app.get("/")
async def root():
    """
    Root endpoint for health check and user-friendly redirect message.
    """
    return {
        "status": "online",
        "message": "Timezone API is running! Navigate to /docs for the interactive Swagger UI.",
        "endpoints": ["/api/v1/time", "/api/v1/timezones"]
    }


class TimezoneInfo(BaseModel):
    timezone: str
    countries: List[str]


class TimezoneListResponse(BaseModel):
    timezones: List[TimezoneInfo]


@app.get("/api/v1/timezones", response_model=TimezoneListResponse)
async def list_timezones():
    """
    Returns a complete structural list of all valid IANA timezones and their roughly associated countries.
    """
    return TimezoneListResponse(timezones=cached_timezone_list)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, log_level="info")
