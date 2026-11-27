"""Connectivity smoke worker; deliberately never reads or writes repository state."""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class SmokeError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPEN = urllib.request.build_opener(NoRedirect()).open
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _url(value, label):
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        raise SmokeError(f"invalid {label}") from None
    if p.scheme != "https" or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment or any(c.isspace() for c in value):
        raise SmokeError(f"invalid {label}")
    return value.rstrip("/")


def _request(method, url, headers, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPEN(req, timeout=20) as response:
            body = response.read()
            if not 200 <= response.status < 300:
                raise SmokeError(f"HTTP {response.status}")
            try:
                return json.loads(body) if body else None
            except (TypeError, ValueError):
                raise SmokeError("invalid JSON response") from None
    except urllib.error.HTTPError as exc:
        raise SmokeError(f"HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise SmokeError("network request failed") from None
    except OSError:
        raise SmokeError("network request failed") from None


def _webhook(value):
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        raise SmokeError("invalid Discord webhook URL") from None
    hosts = {prefix + domain for prefix in ("", "canary.", "ptb.")
             for domain in ("discord.com", "discordapp.com")}
    if (p.scheme != "https" or p.hostname not in hosts or port not in (None, 443)
            or p.username is not None or p.password is not None or p.query or p.fragment
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+", p.path)):
        raise SmokeError("invalid Discord webhook URL")
    return value


def _config():
    worker_id = os.getenv("WORKER_ID", "").strip()
    if not _ID.fullmatch(worker_id):
        raise SmokeError("invalid WORKER_ID")
    master = os.getenv("MASTER_API_URL", "").strip()
    key = os.getenv("MASTER_API_KEY", "").strip()
    if not key:
        raise SmokeError("MASTER_API_KEY is required")
    return worker_id, _url(master, "MASTER_API_URL"), key


def main():
    heartbeat = False
    try:
        worker_id, master, key = _config()
        headers = {"Authorization": f"Bearer {key}", "X-Worker-ID": worker_id,
                   "Content-Type": "application/json"}
        print("[1/4] validating worker authorization")
        repos = _request("GET", f"{master}/api/workers/{worker_id}/repos", headers)
        if not isinstance(repos, list) and not (isinstance(repos, dict) and isinstance(repos.get("repos"), list)):
            raise SmokeError("invalid repository response")
        state = _request("GET", f"{master}/api/state", headers)
        if not isinstance(state, dict) or not isinstance(state.get("state"), dict):
            raise SmokeError("invalid state response")
        print("[2/4] recording heartbeat")
        _request("POST", f"{master}/api/state", headers,
                 {"worker_id": worker_id, "updated_states": {}})
        heartbeat = True
        print("[OK] heartbeat recorded (updated_states={})")
        secret = os.getenv("TEST_WEBHOOK_SECRET", "").strip()
        thread = os.getenv("TEST_THREAD_ID", "").strip()
        if thread and not re.fullmatch(r"[0-9]{1,20}", thread):
            raise SmokeError("invalid TEST_THREAD_ID")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,99}", secret):
            raise SmokeError("invalid TEST_WEBHOOK_SECRET environment name")
        value = os.getenv("TEST_WEBHOOK_VALUE", "").strip()
        if not value:
            raise SmokeError("Discord webhook credential is missing")
        webhook = _webhook(value)
        print("[3/4] sending Discord test")
        suffix = f"?thread_id={thread}" if thread else ""
        _request("POST", webhook + suffix, {"Content-Type": "application/json"},
                 {"content": "[WORKER SMOKE TEST] connectivity verified", "allowed_mentions": {"parse": []}})
        print("[OK] Discord test sent")
        return 0
    except SmokeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        if heartbeat:
            print("[INFO] heartbeat remains recorded", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
