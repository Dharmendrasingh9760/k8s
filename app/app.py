"""
Tiny demo web service used to prove out the monitoring/alerting stack.

Endpoints:
  GET /                -> health check + basic info (proves the service is alive)
  GET /work             -> does some real request handling (fibonacci calc), for
                           a "normal traffic" baseline in the dashboard
  GET /load-cpu?seconds=60          -> pins one CPU core busy for N seconds
                                       (use this to trigger the CPU alerts)
  GET /load-memory?mb=300&seconds=60 -> allocates & holds N MB of RAM for N seconds
                                       (use this to trigger the RAM alerts)
  GET /trigger-error?rate=50&seconds=60 -> starts returning HTTP 500s at ~rate%
                                       of requests for N seconds, to demonstrate
                                       the ERROR ESCALATION alerting path
  GET /stop             -> signals any running load loops / error injection to stop
  GET /metrics           -> Prometheus scrape endpoint (request counts, error
                           counts, latency - auto-instrumented)
"""
import os
import time
import random
import threading
from flask import Flask, request, jsonify, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
SERVICE_NAME = os.environ.get("SERVICE_NAME", "web-service")
_stop_flag = threading.Event()
_error_injection = {"active": False, "rate": 0}

# ---------------------------------------------------------------------
# Prometheus metrics - this is what makes "error escalation" possible.
# Every request is counted by service + endpoint + http status code.
# Alertmanager/Prometheus alert rules then watch the ratio of 5xx to
# total requests and escalate accordingly (see prometheus/alert_rules.yml).
# ---------------------------------------------------------------------
REQUEST_COUNT = Counter(
    "app_http_requests_total",
    "Total HTTP requests processed",
    ["service", "endpoint", "http_status"],
)
REQUEST_LATENCY = Histogram(
    "app_http_request_duration_seconds",
    "Request latency",
    ["service", "endpoint"],
)


@app.before_request
def _start_timer():
    request._start_time = time.time()


@app.after_request
def _record_metrics(response):
    endpoint = request.path
    REQUEST_COUNT.labels(service=SERVICE_NAME, endpoint=endpoint, http_status=response.status_code).inc()
    if hasattr(request, "_start_time"):
        REQUEST_LATENCY.labels(service=SERVICE_NAME, endpoint=endpoint).observe(time.time() - request._start_time)
    return response


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/")
def health():
    return jsonify(
        service=SERVICE_NAME,
        status="healthy",
        uptime_pid=os.getpid(),
    )


@app.route("/work")
def work():
    # Randomly fail if error injection is active - simulates a real
    # service degrading (e.g. a downstream dependency flapping).
    if _error_injection["active"] and random.randint(1, 100) <= _error_injection["rate"]:
        return jsonify(service=SERVICE_NAME, error="simulated internal error"), 500

    n = int(request.args.get("n", 28))
    result = _fib(n)
    return jsonify(service=SERVICE_NAME, fib_input=n, fib_result=result)


@app.route("/load-cpu")
def load_cpu():
    seconds = int(request.args.get("seconds", 60))
    _stop_flag.clear()
    threading.Thread(target=_burn_cpu, args=(seconds,), daemon=True).start()
    return jsonify(
        service=SERVICE_NAME,
        message=f"Started CPU load for {seconds}s. Watch Grafana / Prometheus alerts now.",
    )


@app.route("/load-memory")
def load_memory():
    mb = int(request.args.get("mb", 300))
    seconds = int(request.args.get("seconds", 60))
    _stop_flag.clear()
    threading.Thread(target=_hold_memory, args=(mb, seconds), daemon=True).start()
    return jsonify(
        service=SERVICE_NAME,
        message=f"Allocating {mb}MB for {seconds}s. Watch Grafana / Prometheus alerts now.",
    )


@app.route("/trigger-error")
def trigger_error():
    """Makes /work start returning HTTP 500s at `rate`% for `seconds`,
    so you can demonstrate the error-rate alert + escalation path."""
    rate = int(request.args.get("rate", 50))       # % of requests that fail
    seconds = int(request.args.get("seconds", 120))
    _error_injection["active"] = True
    _error_injection["rate"] = rate
    _stop_flag.clear()
    threading.Thread(target=_error_injection_timer, args=(seconds,), daemon=True).start()
    return jsonify(
        service=SERVICE_NAME,
        message=(
            f"Injecting ~{rate}% error rate into /work for {seconds}s. "
            f"Hit /work repeatedly (or use a load generator) to produce traffic, "
            f"and watch the HighErrorRate alerts escalate."
        ),
    )


@app.route("/stop")
def stop():
    _stop_flag.set()
    _error_injection["active"] = False
    return jsonify(service=SERVICE_NAME, message="Stop signal sent to any running load loops / error injection.")


def _error_injection_timer(seconds):
    end = time.time() + seconds
    while time.time() < end and not _stop_flag.is_set():
        time.sleep(1)
    _error_injection["active"] = False


def _fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _burn_cpu(seconds):
    end = time.time() + seconds
    while time.time() < end and not _stop_flag.is_set():
        # tight busy loop = keeps a CPU core pegged at ~100%
        x = 0
        for i in range(2_000_000):
            x += i * i


def _hold_memory(mb, seconds):
    block = bytearray(mb * 1024 * 1024)  # allocate
    for i in range(0, len(block), 4096):  # touch every page so it's really resident
        block[i] = 1
    end = time.time() + seconds
    while time.time() < end and not _stop_flag.is_set():
        time.sleep(1)
    del block


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
