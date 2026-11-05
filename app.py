
from flask import Flask, render_template, request
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import os

app = Flask(__name__)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
API_KEY = os.getenv("OPENAQ_API_KEY")


# 1. Find city coordinates
def get_city(city):

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
        raise ValueError("City not found. Try another city name.")

    return results[0]


# 2. Find nearest OpenAQ monitoring station
def get_nearest_station(latitude, longitude):

    if not API_KEY:
        raise ValueError("OpenAQ API key missing in .env")

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

    suitable = []

    for location in locations:

        sensors = location.get("sensors", [])

        for sensor in sensors:

            name = sensor.get("parameter", {}).get("name", "")
            name = name.lower().replace(".", "")

            if name == "pm25":
                suitable.append(location)
                break

    if not suitable:
        return None

    suitable.sort(
        key=lambda x: x.get("distance")
        if x.get("distance") is not None else 999999
    )

    return suitable[0]


# 3. Fetch live pollution readings
def get_live_data(station_id):

    url = f"https://api.openaq.org/v3/locations/{station_id}/sensors"

    headers = {"X-API-Key": API_KEY}

    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()

    readings = {}

    for sensor in response.json().get("results", []):

        name = sensor.get("parameter", {}).get("name", "")
        name = name.lower().replace(".", "")

        latest = sensor.get("latest") or {}

        value = latest.get("value")
        time = (latest.get("datetime") or {}).get("utc")

        if name not in ["pm25", "pm10"]:
            continue

        if value is None or time is None:
            continue

        if name not in readings or time > readings[name]["time"]:

            readings[name] = {
                "value": value,
                "time": time
            }

    return readings


# 4. Get pollution forecast and best time
def get_forecast(latitude, longitude, timezone, duration):

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

    options = []

    for i, time in enumerate(hourly["time"]):

        forecast_time = datetime.fromisoformat(time)

        pm25 = hourly["pm2_5"][i]
        pm10 = hourly["pm10"][i]

        if forecast_time >= now and pm25 is not None:

            options.append({
                "time": forecast_time,
                "pm25": pm25,
                "pm10": pm10
            })

    if len(options) < duration:
        raise ValueError("Not enough forecast data available.")

    windows = []

    for i in range(len(options) - duration + 1):

        window = options[i:i + duration]

        # Check consecutive hours
        consecutive = all(
            window[j]["time"] - window[j - 1]["time"]
            == timedelta(hours=1)
            for j in range(1, duration)
        )

        if not consecutive:
            continue

        avg25 = sum(x["pm25"] for x in window) / duration

        pm10_values = [
            x["pm10"] for x in window if x["pm10"] is not None
        ]

        avg10 = (
            sum(pm10_values) / len(pm10_values)
            if pm10_values else None
        )

        windows.append({
            "start": window[0]["time"],
            "end": window[0]["time"] + timedelta(hours=duration),
            "pm25": avg25,
            "pm10": avg10
        })

    if not windows:
        raise ValueError("No complete forecast window available.")

    lowest = min(x["pm25"] for x in windows)
    highest = max(x["pm25"] for x in windows)

    for item in windows:

        if highest == lowest:
            item["score"] = 100
        else:
            item["score"] = round(
                100 * (highest - item["pm25"]) / (highest - lowest)
            )

    return min(windows, key=lambda x: x["pm25"])


# 5. Display measurement time in city timezone
def format_time(value, timezone):

    if not value:
        return "Unavailable"

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    return dt.astimezone(
        ZoneInfo(timezone)
    ).strftime("%d %b %Y, %I:%M %p")


# 6. Main website
@app.route("/")
def home():

    city = request.args.get("city", "Jaipur, Rajasthan").strip()
    activity = request.args.get("activity", "Walking")
    duration = request.args.get("duration", 1, type=int)

    if activity not in ["Walking", "Running", "Cycling"]:
        activity = "Walking"

    if duration not in [1, 2, 3]:
        duration = 1

    try:

        place = get_city(city)

        latitude = place["latitude"]
        longitude = place["longitude"]
        timezone = place.get("timezone") or "Asia/Kolkata"

        station = get_nearest_station(latitude, longitude)

        readings = (
            get_live_data(station["id"])
            if station else {}
        )

        best = get_forecast(
            latitude, longitude, timezone, duration
        )

        data = {
            "city": place["name"],
            "state": place.get("admin1", ""),
            "station": station["name"] if station else "No station within 25 km",

            "pm25": readings.get("pm25", {}).get("value", "Unavailable"),
            "pm10": readings.get("pm10", {}).get("value", "Unavailable"),

            "time25": format_time(
                readings.get("pm25", {}).get("time"), timezone
            ),
            "time10": format_time(
                readings.get("pm10", {}).get("time"), timezone
            ),

            "activity": activity,
            "duration": duration,

            "best_time": best["start"].strftime("%d %b, %I:%M %p"),
            "end_time": best["end"].strftime("%I:%M %p"),
            "best_pm25": round(best["pm25"], 2),
            "best_pm10": round(best["pm10"], 2)
            if best["pm10"] is not None else "Unavailable"
        }

        return render_template(
            "index.html",
            data=data,
            city=city,
            activity=activity,
            duration=duration,
            error=None
        )

    except Exception as error:

        return render_template(
            "index.html",
            data=None,
            city=city,
            activity=activity,
            duration=duration,
            error=str(error)
        )


if __name__ == "__main__":
    app.run(debug=True)
