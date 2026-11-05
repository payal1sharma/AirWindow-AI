

from flask import Flask, render_template, request
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests
import math
import os

app = Flask(__name__)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

API_KEY = os.getenv("OPENAQ_API_KEY")


# 1. Find the selected city
def find_city(city):

    url = "https://geocoding-api.open-meteo.com/v1/search"

    params = {
        "name": city,
        "count": 5,
        "language": "en",
        "countryCode": "IN"
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()

    results = response.json().get("results", [])

    if not results:
        raise ValueError("City not found. Enter city and state.")

    return results[0]


# 2. Calculate distance between two locations
def calculate_distance(lat1, lon1, lat2, lon2):

    radius = 6371

    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)

    dlat = lat2 - lat1
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    return radius * 2 * math.asin(math.sqrt(a))


# 3. Find the nearest monitoring station
def find_station(latitude, longitude):

    url = "https://api.openaq.org/v3/locations"

    params = {
        "coordinates": f"{latitude},{longitude}",
        "radius": 25000,
        "limit": 100
    }

    headers = {"X-API-Key": API_KEY}

    response = requests.get(
        url, params=params, headers=headers, timeout=20
    )

    response.raise_for_status()

    locations = response.json().get("results", [])

    candidates = []

    for location in locations:

        sensors = location.get("sensors", [])

        has_pm25 = any(
            sensor.get("parameter", {}).get("name", "")
            .lower().replace(".", "").replace("_", "") == "pm25"
            for sensor in sensors
        )

        if has_pm25:
            coordinates = location.get("coordinates") or {}

            station_lat = coordinates.get("latitude")
            station_lon = coordinates.get("longitude")

            if station_lat is None or station_lon is None:
                continue

            distance = calculate_distance(
                latitude, longitude, station_lat, station_lon
            )

            candidates.append((distance, location))

    candidates.sort(key=lambda x: x[0])

    # Check nearby stations until recent sensor values are found
    for distance, location in candidates[:10]:

        url = (
            f"https://api.openaq.org/v3/locations/"
            f"{location['id']}/sensors"
        )

        response = requests.get(
            url, headers=headers, timeout=20
        )

        response.raise_for_status()

        readings = {}

        for sensor in response.json().get("results", []):

            name = sensor.get("parameter", {}).get("name", "")
            name = name.lower().replace(".", "").replace("_", "")

            latest = sensor.get("latest") or {}

            value = latest.get("value")
            time = (latest.get("datetime") or {}).get("utc")

            if name not in ["pm25", "pm10"]:
                continue

            if value is None or not time:
                continue

            if name not in readings or time > readings[name]["time"]:
                readings[name] = {
                    "value": value,
                    "time": time
                }

        # Do not call the station current if its PM2.5 is stale
        pm25 = readings.get("pm25")

        if pm25:
            measured_at = datetime.fromisoformat(
                pm25["time"].replace("Z", "+00:00")
            )

            age = datetime.now(timezone.utc) - measured_at

            if age <= timedelta(hours=48):
                return location, distance, readings

    return None, None, {}


# 4. Convert measurement time into local time
def format_time(value, timezone):

    if not value:
        return "Unavailable"

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    return dt.astimezone(
        ZoneInfo(timezone)
    ).strftime("%d %b %Y, %I:%M %p")


# 5. Get forecast for four time periods
def get_forecast(latitude, longitude, timezone):

    url = "https://air-quality-api.open-meteo.com/v1/air-quality"

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "pm2_5,pm10",
        "forecast_days": 2,
        "timezone": timezone
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()

    hourly = response.json()["hourly"]

    now = datetime.now(ZoneInfo(timezone)).replace(tzinfo=None)

    periods = {
        "Morning": [],
        "Day": [],
        "Evening": [],
        "Night": []
    }

    for i in range(len(hourly["time"])):

        time = datetime.fromisoformat(hourly["time"][i])

        pm25 = hourly["pm2_5"][i]
        pm10 = hourly["pm10"][i]

        if time < now or pm25 is None:
            continue

        hour = time.hour

        if 5 <= hour < 11:
            period = "Morning"

        elif 11 <= hour < 16:
            period = "Day"

        elif 16 <= hour < 20:
            period = "Evening"

        else:
            period = "Night"

        periods[period].append({
            "time": time,
            "pm25": pm25,
            "pm10": pm10
        })

    results = []

    for name, values in periods.items():

        if values:

            best = min(values, key=lambda x: x["pm25"])

            results.append({
                "name": name,
                "time": best["time"].strftime("%d %b %Y, %I:%M %p"),
                "pm25": round(best["pm25"], 2),
                "pm10": round(best["pm10"], 2)
                if best["pm10"] is not None else "Unavailable"
            })

        else:

            results.append({
                "name": name,
                "time": "Unavailable",
                "pm25": "Unavailable",
                "pm10": "Unavailable"
            })

    return results


# 6. Main website
@app.route("/")
def home():

    city = request.args.get("city", "Jaipur, Rajasthan").strip()

    try:

        place = find_city(city)

        latitude = place["latitude"]
        longitude = place["longitude"]
        timezone = place["timezone"]

        station, distance, readings = find_station(
            latitude, longitude
        )

        periods = get_forecast(
            latitude, longitude, timezone
        )

        data = {
            "city": place["name"],
            "state": place.get("admin1", ""),
            "country": place.get("country", ""),
            "station": station["name"] if station else "Not available",
            "distance": round(distance, 1) if distance is not None else None,

            "pm25": readings.get("pm25", {}).get("value", "Unavailable"),
            "pm10": readings.get("pm10", {}).get("value", "Unavailable"),

            "time25": format_time(
                readings.get("pm25", {}).get("time"), timezone
            ),
            "time10": format_time(
                readings.get("pm10", {}).get("time"), timezone
            ),

            "periods": periods
        }

        return render_template(
            "index.html",
            data=data,
            city=city,
            error=None
        )

    except Exception as error:

        return render_template(
            "index.html",
            data=None,
            city=city,
            error=str(error)
        )


if __name__ == "__main__":
    app.run(debug=True)
