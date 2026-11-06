"""Forecast data from Open-Meteo (no API key), with a built-in fallback."""

import requests

CITIES = {
    "London": (51.51, -0.13),
    "New York": (40.71, -74.01),
    "Bengaluru": (12.97, 77.59),
    "Tokyo": (35.68, 139.69),
}

FALLBACK = [
    {"date": "Day 1", "max": 18, "min": 11, "rain": 0.4},
    {"date": "Day 2", "max": 20, "min": 12, "rain": 0.0},
    {"date": "Day 3", "max": 17, "min": 10, "rain": 2.1},
]


def get_forecast(city: str) -> tuple[list[dict], str]:
    lat, lon = CITIES[city]
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "forecast_days": 3,
                "timezone": "auto",
            },
            timeout=5,
        )
        resp.raise_for_status()
        daily = resp.json()["daily"]
    except (requests.RequestException, KeyError, ValueError):
        return FALLBACK, "built-in sample data (Open-Meteo unreachable)"
    days = [
        {"date": date, "max": round(tmax), "min": round(tmin), "rain": rain}
        for date, tmax, tmin, rain in zip(
            daily["time"],
            daily["temperature_2m_max"],
            daily["temperature_2m_min"],
            daily["precipitation_sum"],
            strict=False,
        )
    ]
    return days, "Open-Meteo"
