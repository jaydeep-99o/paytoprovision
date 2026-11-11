"""Weather Now: a 3-day forecast for a few cities."""

from flask import Flask, render_template_string, request
from forecast import CITIES, get_forecast

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weather Now</title>
<style>
  body { font-family: system-ui, sans-serif; background: linear-gradient(#e0f2fe, #f8fafc); margin: 0; color: #0f172a; }
  main { max-width: 640px; margin: 0 auto; padding: 36px 16px; }
  select, button { padding: 9px 12px; border-radius: 9px; border: 1px solid #cbd5e1; font-size: 15px; }
  button { background: #0284c7; color: #fff; border: 0; font-weight: 600; }
  .days { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 20px; }
  .day { background: #fff; border-radius: 14px; padding: 16px; text-align: center; box-shadow: 0 1px 3px #0001; }
  .t { font-size: 28px; font-weight: 700; } .muted { color: #64748b; font-size: 14px; }
</style></head>
<body><main>
  <h1>Weather Now</h1>
  <form><select name="city">{% for c in cities %}<option {% if c == city %}selected{% endif %}>{{ c }}</option>{% endfor %}</select>
  <button>Show forecast</button></form>
  <div class="days">{% for d in days %}
    <div class="day"><div class="muted">{{ d.date }}</div><div class="t">{{ d.max }}°</div>
    <div class="muted">low {{ d.min }}° · rain {{ d.rain }} mm</div></div>{% endfor %}</div>
  <p class="muted">Source: {{ data_source }}</p>
</main></body></html>"""


@app.get("/")
def index():
    city = request.args.get("city", "London")
    if city not in CITIES:
        city = "London"
    days, source = get_forecast(city)
    return render_template_string(PAGE, cities=CITIES, city=city, days=days, data_source=source)


if __name__ == "__main__":
    app.run(port=5000)
