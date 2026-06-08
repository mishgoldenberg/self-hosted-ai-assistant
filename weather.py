"""
weather.py — Current + forecast weather via Open-Meteo (free, no API key).

City is stored in memory.db as category='config', fact starting with 'city='.
Fall back to a hardcoded default when no config entry exists.

Public API:
    get_weather(date_keyword)  →  str  (display-ready block or error note)
    set_city(city_name)        →  str  (confirmation message)
    get_city()                 →  str  (current city name)
"""

import json
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, timedelta

import memory as _memory

_DEFAULT_CITY = "London"   # Change via /weather setdefault <city>, or edit this default here
_TIMEOUT = 8   # seconds for each HTTP call


# ── Config helpers ─────────────────────────────────────────────────────────────

def get_city() -> str:
    """Return the configured default city, falling back to the hardcoded default."""
    for m in _memory.get_all():
        if m["category"] == "config" and m["fact"].startswith("city="):
            return m["fact"][5:].strip()
    return _DEFAULT_CITY


def set_city(city_name: str) -> str:
    """
    Persist city as the new default.  Validates that the city can be geocoded
    before saving — returns an error string if the city is not found.
    """
    city_name = city_name.strip()
    geo = _geocode(city_name)
    if geo is None:
        return f"⚠️ Couldn't find a city called \"{city_name}\" — check the spelling and try again."
    _lat, _lon, resolved = geo
    # Remove any existing city config entry
    for m in _memory.get_all():
        if m["category"] == "config" and m["fact"].startswith("city="):
            _memory.forget(m["id"])
    result = _memory.add(f"city={city_name}", category="config")
    if "error" in result:
        return f"⚠️ Could not save city: {result['error']}"
    return f"✅ Default weather city set to: {resolved}"


# ── Geocoding ──────────────────────────────────────────────────────────────────

def _geocode(city: str) -> tuple[float, float, str] | None:
    """
    Return (lat, lon, resolved_name) or None on failure.
    Strips trailing country suffix (e.g. ", Israel") before querying,
    and retries with the alternate "Qiryat" transliteration when "Kiryat" fails.
    """
    # Strip ", Country" suffix — the geocoder works better without it
    name = re.sub(r',\s*[A-Za-z ]+$', '', city).strip()

    def _query(q: str) -> tuple[float, float, str] | None:
        params = urllib.parse.urlencode({"name": q, "count": 1, "language": "en", "format": "json"})
        url = f"https://geocoding-api.open-meteo.com/v1/search?{params}"
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read())
            results = data.get("results")
            if not results:
                return None
            r = results[0]
            rname = r.get("name", q)
            country = r.get("country_code", "")
            return r["latitude"], r["longitude"], f"{rname}, {country}"
        except Exception:
            return None

    result = _query(name)
    if result:
        return result

    # Retry with Hebrew transliteration alias: "Kiryat" → "Qiryat"
    alt = re.sub(r'\bKiryat\b', 'Qiryat', name, flags=re.IGNORECASE)
    if alt != name:
        result = _query(alt)
        if result:
            return result

    return None


# ── Weather fetch ──────────────────────────────────────────────────────────────

_WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Thunderstorm + heavy hail",
}


def _fetch_weather(lat: float, lon: float, include_tomorrow: bool = False) -> dict:
    """Fetch current conditions + daily summary from Open-Meteo."""
    params = urllib.parse.urlencode({
        "latitude":              lat,
        "longitude":             lon,
        "current":               "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily":                 "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
        "forecast_days":         2,
        "timezone":              "auto",
        "wind_speed_unit":       "kmh",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())


# ── Formatter ──────────────────────────────────────────────────────────────────

def _fmt_weather_block(data: dict, city_label: str,
                       include_tomorrow: bool = False) -> str:
    cur = data.get("current", {})
    daily = data.get("daily", {})

    temp      = cur.get("temperature_2m", "?")
    feels     = cur.get("apparent_temperature", "?")
    code      = cur.get("weather_code", 0)
    wind      = cur.get("wind_speed_10m", "?")
    humidity  = cur.get("relative_humidity_2m", "?")
    condition = _WMO_CODES.get(code, f"code {code}")

    today_max = daily.get("temperature_2m_max", [None])[0]
    today_min = daily.get("temperature_2m_min", [None])[0]
    today_rain = daily.get("precipitation_sum", [None])[0]

    lines = [
        f"🌤 Weather — {city_label}",
        f"   {condition}  {temp}°C (feels {feels}°C)",
        f"   High {today_max}° / Low {today_min}°  💧{today_rain} mm  💨 {wind} km/h  💧{humidity}%",
    ]

    if include_tomorrow and len(daily.get("time", [])) >= 2:
        t_code  = (daily.get("weather_code")    or [0, 0])[1]
        t_max   = (daily.get("temperature_2m_max") or [None, None])[1]
        t_min   = (daily.get("temperature_2m_min") or [None, None])[1]
        t_rain  = (daily.get("precipitation_sum")  or [None, None])[1]
        t_cond  = _WMO_CODES.get(t_code, f"code {t_code}")
        lines.append(
            f"   Tomorrow: {t_cond}  High {t_max}° / Low {t_min}°  💧{t_rain} mm"
        )

    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────────

def get_weather(include_tomorrow: bool = False, city: str | None = None) -> str:
    """
    Return a display-ready weather block.

    city: if given, fetch weather for that city instead of the stored default.
          An unrecognised city returns a user-friendly "not found" message.
    include_tomorrow: also show tomorrow's forecast line.
    Never raises — returns an informative error note on any failure.
    """
    target = city.strip() if city else get_city()
    try:
        geo = _geocode(target)
        if geo is None:
            if city:
                return f"⚠️ Couldn't find a city called \"{target}\" — check the spelling and try again."
            return f"⚠️ Weather: couldn't geocode the default city \"{target}\""
        lat, lon, label = geo
        data = _fetch_weather(lat, lon, include_tomorrow)
        return _fmt_weather_block(data, label, include_tomorrow)
    except urllib.error.URLError as exc:
        return f"⚠️ Weather unavailable (network error: {exc.reason})"
    except Exception as exc:
        return f"⚠️ Weather unavailable ({type(exc).__name__}: {exc})"
