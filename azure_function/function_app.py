"""
Azure Functions entry points for the Jerusalem job agent.

- daily_update (timer): runs at 16:30 and 17:30 UTC. One of the two is 19:30 in
  Israel (winter / summer); job_agent.run() sends only from 19:00 Israel time
  and only once per day, so exactly one message goes out.
- run_now (HTTP, function key): a manual run, for testing or to resend today.

Each sent update pings Healthchecks.io (HEALTHCHECK_URL). If a day passes with
no ping, Healthchecks.io emails an alert. A crash pings /fail right away.
"""
import logging

import azure.functions as func
import requests

import job_agent

app = func.FunctionApp()


def _ping(suffix=""):
    url = job_agent.os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        requests.get(url.rstrip("/") + suffix, timeout=10)
    except requests.RequestException as e:
        logging.warning("Healthchecks ping failed: %s", e)


def _run(manual):
    try:
        status = job_agent.run(manual=manual)
    except Exception:
        logging.exception("Job agent run failed")
        _ping("/fail")
        raise
    logging.info(status)
    if status.startswith("sent"):
        _ping()
    return status


@app.timer_trigger(arg_name="timer", schedule="0 30 16,17 * * *", run_on_startup=False)
def daily_update(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("Timer is running late")
    _run(manual=False)


@app.route(route="run_now", auth_level=func.AuthLevel.FUNCTION, methods=["GET", "POST"])
def run_now(req: func.HttpRequest) -> func.HttpResponse:
    status = _run(manual=True)
    return func.HttpResponse(status + "\n", mimetype="text/plain")
