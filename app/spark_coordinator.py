"""Isolated Spark gateway, independent of the application's document backend."""
import os
import requests


def configured():
    return bool(os.environ.get("SPARK_COORDINATOR_BASE_URL") and os.environ.get("SPARK_COORDINATOR_SECRET"))


def request(path, payload=None, *, method="POST", params=None):
    base = os.environ["SPARK_COORDINATOR_BASE_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("Spark coordinator requires HTTPS")
    response = requests.request(method, base + path, json=payload, params=params,
        headers={"x-internal-api-key": os.environ["SPARK_COORDINATOR_SECRET"]},
        timeout=(3, 8), allow_redirects=False)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("Invalid Spark coordinator response")
    return result
