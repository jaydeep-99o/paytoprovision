"""Invoice Studio: create and preview invoices with Acme's billing engine."""

import os

from acme_billing import InvoiceEngine
from flask import Flask, render_template_string, request

app = Flask(__name__)
engine = InvoiceEngine(api_key=os.environ.get("ACME_BILLING_KEY", ""))

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Invoice Studio</title></head>
<body><h1>Invoice Studio</h1>
<form method="post"><input name="customer" placeholder="Customer"><input name="amount" placeholder="Amount">
<button>Preview invoice</button></form>
{% if invoice %}<pre>{{ invoice }}</pre>{% endif %}</body></html>"""


@app.route("/", methods=["GET", "POST"])
def index():
    invoice = None
    if request.method == "POST":
        invoice = engine.render(customer=request.form["customer"], amount=request.form["amount"])
    return render_template_string(PAGE, invoice=invoice)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
