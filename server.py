"""Monitor: self-hosted error tracking that speaks the Sentry SDK wire protocol.

Apps keep the official SDKs (@sentry/nextjs, @sentry/react-native, sentry-sdk) and point their DSN here.
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
import zlib
from email.message import EmailMessage
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("MONITOR_DB", HERE / "data" / "monitor.db"))
TOKEN = os.environ.get("MONITOR_TOKEN", "")  # dashboard password
KEY = os.environ.get("MONITOR_KEY", "")  # public DSN key the SDKs send
if len(TOKEN) < 16 or len(KEY) < 16:
    raise SystemExit("MONITOR_TOKEN / MONITOR_KEY missing or too short in .env. Run ./setup.sh.")
# "1=web,2=mobile,3=api" -> names shown instead of project ids
PROJECTS = dict(p.strip().split("=", 1) for p in os.environ.get("MONITOR_PROJECTS", "").split(",") if "=" in p)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "").strip()
MAX_BODY = 5_000_000
KEEP_EVENTS = 50  # per issue

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)  # only touched from the event loop thread
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists issues(
  id integer primary key, project text, fingerprint text unique, title text, culprit text,
  level text, platform text, environment text, count int, first_seen real, last_seen real,
  status text default 'unresolved');
create table if not exists events(id integer primary key, issue_id int, received real, data text);
create index if not exists events_issue on events(issue_id, id);
""")

app = FastAPI()
# Browser SDKs post cross-origin from your sites.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
listeners: set[asyncio.Queue] = set()


# ---------- protocol ----------

def parse_envelope(body: bytes):
    """Envelope = header line, then (item header line, payload) pairs. Payload length is optional."""
    header_line, _, rest = body.partition(b"\n")
    header = json.loads(header_line or b"{}")
    items = []
    while rest.strip():
        ih_line, _, rest = rest.partition(b"\n")
        if not ih_line.strip():
            continue
        ih = json.loads(ih_line)
        if ih.get("length") is not None:
            n = int(ih["length"])
            payload, rest = rest[:n], rest[n:]
            rest = rest[1:] if rest[:1] == b"\n" else rest
        else:
            payload, _, rest = rest.partition(b"\n")
        items.append((ih, payload))
    return header, items


def decode(body: bytes, encoding: str | None) -> bytes:
    encoding = (encoding or "").lower()
    if encoding in ("gzip", "deflate"):
        d = zlib.decompressobj(47)  # auto-detect gzip/zlib header
        out = d.decompress(body, MAX_BODY)
        if d.unconsumed_tail:
            raise HTTPException(413, "payload too large")
        return out
    if encoding == "br":  # sentry-sdk (Python) uses brotli when the app has it installed
        import brotli
        d, out = brotli.Decompressor(), bytearray()
        # ponytail: 256-byte input steps bound bomb expansion per step, not exactly; fine behind the key check
        for i in range(0, len(body), 256):
            out += d.process(body[i:i + 256])
            if len(out) > MAX_BODY:
                raise HTTPException(413, "payload too large")
        return bytes(out)
    return body


def check_key(req: Request):
    m = re.search(r"sentry_key=([^,\s&]+)", req.headers.get("x-sentry-auth", ""))
    given = req.query_params.get("key") or req.query_params.get("sentry_key") or (m.group(1) if m else "")
    if not secrets.compare_digest(given, KEY):
        raise HTTPException(401, "bad sentry_key")


# ---------- grouping & storage ----------

def group(event: dict):
    """-> (title, culprit, grouping key). Same key = same issue."""
    excs = (event.get("exception") or {}).get("values") or []
    if excs:
        e = excs[-1]  # outermost exception
        frames = (e.get("stacktrace") or {}).get("frames") or []
        top = ([f for f in frames if f.get("in_app")] or frames or [{}])[-1]
        where = top.get("filename") or top.get("module") or top.get("abs_path")
        # bundle names carry a per-build hash (app-3f9a1c2b.js); drop it so a deploy doesn't re-open every issue
        where = where and re.sub(r"[-.][0-9a-f]{8,}(?=\.)|\?.*$|^https?://[^/]+", "", where)
        title = f'{e.get("type") or "Error"}: {e.get("value") or ""}'.rstrip(": ")
        culprit = f'{top.get("function") or "?"} in {where}' if where else ""
        key = [e.get("type"), where, top.get("function")] if where else [e.get("type"), e.get("value")]
    else:
        msg = event.get("logentry") or event.get("message") or "Unknown event"
        if isinstance(msg, dict):
            msg = msg.get("formatted") or msg.get("message") or "Unknown event"
        title, culprit, key = str(msg), event.get("transaction") or "", [str(msg)]
    fp = event.get("fingerprint")
    if fp and fp != ["{{ default }}"]:
        key = fp
    return title[:300], culprit[:300], key


def store(project: str, event: dict):
    title, culprit, key = group(event)
    fp = hashlib.sha1(json.dumps([project, key], default=str).encode()).hexdigest()
    now = time.time()
    row = db.execute("select id, status from issues where fingerprint=?", (fp,)).fetchone()
    if row:
        iid = row["id"]
        alert = "regression" if row["status"] == "resolved" else None
        db.execute(
            "update issues set count=count+1, last_seen=?, title=?, environment=?,"
            " status=case when status='resolved' then 'unresolved' else status end where id=?",
            (now, title, event.get("environment"), iid))
    else:
        iid = db.execute(
            "insert into issues(project,fingerprint,title,culprit,level,platform,environment,count,first_seen,last_seen)"
            " values(?,?,?,?,?,?,?,1,?,?)",
            (project, fp, title, culprit, event.get("level") or "error", event.get("platform"),
             event.get("environment"), now, now)).lastrowid
        alert = "new"
    db.execute("insert into events(issue_id, received, data) values(?,?,?)", (iid, now, json.dumps(event)))
    db.execute("delete from events where issue_id=? and id not in"
               " (select id from events where issue_id=? order by id desc limit ?)", (iid, iid, KEEP_EVENTS))
    db.commit()
    return iid, alert


def issue_dict(iid: int) -> dict:
    d = dict(db.execute("select * from issues where id=?", (iid,)).fetchone())
    d["project_name"] = PROJECTS.get(d["project"], f'project {d["project"]}')
    return d


# ---------- alerts ----------

def send_email(issue: dict, alert: str, event: dict):
    user, pw = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    if not (user and pw and ALERT_EMAIL):
        return
    port = int(os.environ.get("SMTP_PORT", "465"))
    req, usr = event.get("request") or {}, event.get("user") or {}
    lines = [
        f'{"New error" if alert == "new" else "Error is back (was resolved)"} in {issue["project_name"]}',
        "", issue["title"], issue["culprit"], "",
        f'Environment: {event.get("environment") or "-"}   Release: {event.get("release") or "-"}',
        f'Platform: {event.get("platform") or "-"}   Level: {issue["level"]}',
    ]
    if req.get("url"):
        lines.append(f'URL: {req.get("method", "")} {req["url"]}')
    if usr:
        lines.append(f'User: {usr.get("email") or usr.get("username") or usr.get("id") or usr.get("ip_address")}')
    if PUBLIC_URL:
        lines += ["", f'Open: {PUBLIC_URL}/#issue-{issue["id"]}']
    msg = EmailMessage()
    msg["Subject"] = f'[{issue["project_name"]}] {"New" if alert == "new" else "Regressed"}: {issue["title"][:120]}'
    msg["From"], msg["To"] = user, ALERT_EMAIL
    msg.set_content("\n".join(lines))
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)
    except Exception as e:  # never let a mail outage break ingestion
        print("alert email failed:", e)


async def ingest(project: str, event: dict):
    iid, alert = store(project, event)
    issue = issue_dict(iid)
    for q in list(listeners):
        q.put_nowait({"issue": issue, "alert": alert})
    if alert:
        threading.Thread(target=send_email, args=(issue, alert, event), daemon=True).start()


# ---------- ingest endpoints (what the SDKs call) ----------

async def read_body(req: Request) -> bytes:
    body = await req.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "payload too large")
    return decode(body, req.headers.get("content-encoding"))


@app.post("/api/{project}/envelope/")
async def envelope(project: str, req: Request):
    check_key(req)
    header, items = parse_envelope(await read_body(req))
    for ih, payload in items:
        if ih.get("type") == "event":  # errors; transactions/sessions/replays are accepted and dropped
            await ingest(project, json.loads(payload))
    return {"id": header.get("event_id")}


@app.post("/ingest/{project}")  # monitor-js / monitor-sdk
@app.post("/api/{project}/store/")  # legacy Sentry-format SDKs
async def store_event(project: str, req: Request):
    check_key(req)
    event = json.loads(await read_body(req))
    await ingest(project, event)
    return {"id": event.get("event_id")}


# ---------- dashboard ----------

def check_token(token: str):
    if not secrets.compare_digest(token or "", TOKEN):
        raise HTTPException(401, "bad token")


@app.get("/")
async def dashboard():
    return FileResponse(HERE / "dashboard.html")


@app.get("/d/issues")
async def list_issues(token: str = "", status: str = "unresolved"):
    check_token(token)
    rows = db.execute("select id from issues where status=? order by last_seen desc limit 500", (status,))
    return [issue_dict(r["id"]) for r in rows.fetchall()]


@app.get("/d/overview")
async def overview(token: str = ""):
    """Per project: issue counts by status, total events, and events per hour for the last 24h."""
    check_token(token)
    now = time.time()
    projects: dict[str, dict] = {}

    def get(pid):
        return projects.setdefault(pid, {"id": pid, "name": PROJECTS.get(pid, f"project {pid}"),
                                         "unresolved": 0, "resolved": 0, "ignored": 0, "events": 0,
                                         "hourly": [0] * 24})

    for pid in PROJECTS:
        get(pid)
    for r in db.execute("select project, status, count(*) n, sum(count) ev from issues group by project, status"):
        p = get(r["project"])
        p[r["status"]] = r["n"]
        p["events"] += r["ev"] or 0
    for r in db.execute(
            "select i.project, cast((? - e.received) / 3600 as int) h, count(*) n from events e"
            " join issues i on i.id = e.issue_id where e.received > ? group by 1, 2", (now, now - 86400)):
        get(r["project"])["hourly"][23 - min(r["h"], 23)] += r["n"]
    return sorted(projects.values(), key=lambda p: (not p["id"].isdigit(), int(p["id"]) if p["id"].isdigit() else 0))


@app.get("/d/issues/{iid}")
async def get_issue(iid: int, token: str = ""):
    check_token(token)
    if not db.execute("select 1 from issues where id=?", (iid,)).fetchone():
        raise HTTPException(404)
    ev = db.execute("select data from events where issue_id=? order by id desc limit 1", (iid,)).fetchone()
    times = [r[0] for r in db.execute("select received from events where issue_id=? order by id desc", (iid,))]
    return {"issue": issue_dict(iid), "event": json.loads(ev["data"]) if ev else None, "recent": times}


@app.post("/d/issues/{iid}/{status}")
async def set_status(iid: int, status: str, token: str = ""):
    check_token(token)
    if status not in ("unresolved", "resolved", "ignored"):
        raise HTTPException(400)
    db.execute("update issues set status=? where id=?", (status, iid))
    db.commit()
    issue = issue_dict(iid)
    for q in list(listeners):
        q.put_nowait({"issue": issue, "alert": None})
    return issue


@app.get("/d/stream")
async def stream(token: str = ""):
    check_token(token)
    q: asyncio.Queue = asyncio.Queue()
    listeners.add(q)

    async def gen():
        try:
            while True:
                try:
                    yield f"data: {json.dumps(await asyncio.wait_for(q.get(), 15))}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            listeners.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
