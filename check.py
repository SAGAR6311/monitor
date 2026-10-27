"""Self-check for envelope parsing, decompression and issue grouping. Run: .venv/bin/python check.py"""
import gzip
import json
import os
import tempfile

os.environ.update(MONITOR_TOKEN="t" * 16, MONITOR_KEY="k" * 16, MONITOR_DB=tempfile.mktemp(suffix=".db"))
import server  # noqa: E402

# envelope with and without explicit item length, payload containing a newline-free JSON and a binary item
ev = {"event_id": "a" * 32, "exception": {"values": [{"type": "TypeError", "value": "x is undefined",
      "stacktrace": {"frames": [{"filename": "lib.js", "function": "lib"}, {"filename": "app.js", "function": "boom", "in_app": True}]}}]}}
payload = json.dumps(ev).encode()
body = b'{"event_id":"aaa"}\n{"type":"event"}\n' + payload + b'\n{"type":"attachment","length":3}\na\nb\n'
header, items = server.parse_envelope(body)
assert header["event_id"] == "aaa"
assert [i[0]["type"] for i in items] == ["event", "attachment"]
assert json.loads(items[0][1]) == ev and items[1][1] == b"a\nb"
assert server.parse_envelope(server.decode(gzip.compress(body), "gzip")) == (header, items)

title, culprit, key = server.group(ev)
assert title == "TypeError: x is undefined" and culprit == "boom in app.js"
assert server.group({"logentry": {"message": "disk full"}})[0] == "disk full"
assert server.group({**ev, "fingerprint": ["custom"]})[2] == ["custom"]

# same error twice = one issue, alert only first time; resolve then recur = regression
iid, alert = server.store("1", ev)
assert alert == "new"
assert server.store("1", ev) == (iid, None)
assert server.store("2", ev)[1] == "new"  # other project = other issue
server.db.execute("update issues set status='resolved' where id=?", (iid,))
assert server.store("1", ev) == (iid, "regression")
assert server.issue_dict(iid)["count"] == 3 and server.issue_dict(iid)["status"] == "unresolved"
print("ok")

# per-build bundle hashes and origins don't split an issue across deploys
a = {"exception": {"values": [{"type": "E", "stacktrace": {"frames": [{"filename": "https://a.com/_next/static/chunks/app-3f9a1c2b44.js?v=1", "function": "f"}]}}]}}
b = json.loads(json.dumps(a).replace("3f9a1c2b44", "99aa77bb66"))
assert server.group(a)[2] == server.group(b)[2] == ["E", "/_next/static/chunks/app.js", "f"]
print("ok 2")
