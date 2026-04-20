"""
Keep-alive web server for hosting on GitHub Actions / Replit.
Runs a small Flask web server so that an uptime monitor (e.g. cron-job.org)
can ping it and prevent the process from being killed.
"""

from flask import Flask
from threading import Thread

app = Flask("")


@app.route("/")
def home():
    return "WondeX bot is running!"


def run():
    app.run(host="0.0.0.0", port=8080)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
