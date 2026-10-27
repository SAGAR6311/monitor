"""monitor-sdk: reports Python / Django errors to your Monitor server. No dependencies.

    import monitor_sdk
    monitor_sdk.init("https://KEY@monitor.example.com/3", environment="production")
"""
import atexit
import json
import logging
import os
import platform
import queue
import re
import socket
import sys
import threading
import time
import traceback
import urllib.request
import uuid

MAX_PER_MINUTE = 30  # stops an error inside a loop from flooding the server

_cfg: dict = {}
_tags: dict = {}
_user: dict | None = None
_q: queue.Queue = queue.Queue(maxsize=200)
_recent: list[float] = []
_worker_pid = None
_lock = threading.Lock()


def init(dsn, environment=None, release=None, tags=None):
    m = re.match(r"^(https?)://([^@]+)@([^/]+)(/.*)?/(\d+)$", dsn or "")
    if not m:
        raise ValueError("monitor_sdk: invalid dsn, expected https://KEY@host/PROJECT_ID")
    scheme, key, host, path, project = m.groups()
    _cfg.update(url=f"{scheme}://{host}{path or ''}/ingest/{project}?key={key}",
                environment=environment, release=release)
    _tags.update(tags or {})
    if _cfg.get("installed"):
        return
    _cfg["installed"] = True
    atexit.register(flush)

    prev_hook = sys.excepthook
    def excepthook(t, v, tb):
        capture_exception(v, level="fatal")
        flush()
        prev_hook(t, v, tb)
    sys.excepthook = excepthook

    prev_thread_hook = threading.excepthook
    def thread_hook(args):
        capture_exception(args.exc_value)
        prev_thread_hook(args)
    threading.excepthook = thread_hook

    logging.getLogger().addHandler(_LogHandler(logging.ERROR))

    try:  # Django: unhandled view errors (500s)
        from django.core.signals import got_request_exception
        got_request_exception.connect(_django_error, weak=False)
    except ImportError:
        pass


def set_user(user):
    """e.g. set_user({"id": "42", "email": "a@b.c"}); None clears it."""
    global _user
    _user = user


def set_tag(key, value):
    _tags[key] = value


def capture_exception(exc=None, level="error", request=None, extra=None):
    exc = exc or sys.exc_info()[1]
    if exc is None:
        return None
    return _send({"level": level, "exception": {"values": _exception_values(exc)}}, request, extra)


def capture_message(message, level="info", request=None, extra=None, fingerprint=None):
    body = {"level": level, "message": str(message)}
    if fingerprint:
        body["fingerprint"] = fingerprint
    return _send(body, request, extra)


def flush(timeout=2.0):
    """Wait (up to timeout seconds) for queued events to be sent."""
    end = time.time() + timeout
    while _q.unfinished_tasks and time.time() < end:
        time.sleep(0.05)


# ---------- internals ----------

def _in_app(path):
    return not ("site-packages" in path or "dist-packages" in path or path.startswith(sys.base_prefix)
                or path.startswith("<"))


def _short(path):
    cwd = os.getcwd() + os.sep
    return path[len(cwd):] if path.startswith(cwd) else path


def _exception_values(exc):
    values, seen = [], set()
    while exc is not None and id(exc) not in seen and len(values) < 5:
        seen.add(id(exc))
        frames = [{"filename": _short(f.filename), "abs_path": f.filename, "function": f.name,
                   "lineno": f.lineno, "context_line": f.line, "in_app": _in_app(f.filename)}
                  for f in traceback.extract_tb(exc.__traceback__)]
        values.insert(0, {"type": type(exc).__name__, "module": type(exc).__module__,
                          "value": str(exc), "stacktrace": {"frames": frames}})
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return values


def _send(body, request=None, extra=None):
    if not _cfg:
        return None
    now = time.time()
    with _lock:
        _recent[:] = [t for t in _recent if now - t < 60]
        if len(_recent) >= MAX_PER_MINUTE:
            return None
        _recent.append(now)
    request = dict(request or {})
    event = {
        "event_id": uuid.uuid4().hex, "timestamp": now, "platform": "python",
        "environment": _cfg["environment"], "release": _cfg["release"],
        "server_name": socket.gethostname(), "tags": dict(_tags), "extra": extra,
        "user": request.pop("user", None) or _user,
        "request": request or None,
        "contexts": {"runtime": {"name": "python", "version": platform.python_version()},
                     "os": {"name": platform.system(), "version": platform.release()}},
        **body,
    }
    _ensure_worker()
    try:
        _q.put_nowait(event)
    except queue.Full:
        return None
    return event["event_id"]


def _ensure_worker():
    # Started lazily per process: gunicorn/uwsgi fork after init and threads don't survive a fork.
    global _worker_pid
    with _lock:
        if _worker_pid != os.getpid():
            _worker_pid = os.getpid()
            threading.Thread(target=_worker, daemon=True, name="monitor-sdk").start()


def _worker():
    while True:
        event = _q.get()
        try:
            req = urllib.request.Request(_cfg["url"], data=json.dumps(event, default=str).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()
        except Exception:
            pass  # monitoring must never break the app
        finally:
            _q.task_done()


class _LogHandler(logging.Handler):
    """logger.error(...) / logger.exception(...) anywhere in the app becomes an event."""

    def emit(self, record):
        if record.name == "django.request":  # already reported via got_request_exception
            return
        try:
            req = _django_request(getattr(record, "request", None))
            extra = {"logger": record.name, "log": record.getMessage()}
            if record.exc_info and record.exc_info[1]:
                capture_exception(record.exc_info[1], level=record.levelname.lower(), request=req, extra=extra)
            else:  # group by the message template, not the formatted text
                capture_message(record.getMessage(), level=record.levelname.lower(), request=req,
                                extra=extra, fingerprint=[record.name, str(record.msg)])
        except Exception:
            pass


def _django_request(request):
    if request is None:
        return None
    out = {"method": request.method}
    try:
        out["url"] = request.build_absolute_uri()
    except Exception:  # DisallowedHost etc.
        out["url"] = request.path
    out["headers"] = {"User-Agent": request.META.get("HTTP_USER_AGENT", "")}
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        out["user"] = {"id": str(user.pk), "username": user.get_username()}
    return out


def _django_error(sender, request=None, **kwargs):
    capture_exception(request=_django_request(request))
