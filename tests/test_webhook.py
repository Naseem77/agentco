import hashlib
import hmac
import http.client
import json
import socket
import threading
import time
from dataclasses import dataclass

import pytest

import agentco.webhook as webhook_module
from agentco.store.task_store import TaskStore
from agentco.webhook import (
    ACCEPTED_EVENTS,
    IntakeTrigger,
    WebhookRejected,
    check_delivery,
    make_server,
    verify_signature,
)

SECRET = b"s3cret"
REPO = "acme/app"
MAX_BODY = 1000


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def payload(repo: str = REPO, **extra) -> bytes:
    return json.dumps({"repository": {"full_name": repo}, **extra}).encode()


@dataclass
class Resp:
    status: int
    body: bytes


class Rig:
    def __init__(self, store: TaskStore):
        self.calls = 0
        self.trigger = IntakeTrigger(self._run, debounce_s=0)
        self.httpd = make_server(
            "127.0.0.1",
            0,
            tenant="acme",
            store=store,
            repo=REPO,
            secret=SECRET,
            max_body=MAX_BODY,
            trigger=self.trigger,
        )
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, args=(0.01,), daemon=True
        )
        self.thread.start()

    def _run(self):
        self.calls += 1

    def request(self, method="POST", path="/webhook", body=b"", headers=None):
        conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders(body or None)
            r = conn.getresponse()
            return Resp(r.status, r.read())
        finally:
            conn.close()

    def post(
        self,
        body=None,
        *,
        event="issues",
        delivery="d1",
        signature="auto",
        length="auto",
        path="/webhook",
    ):
        body = payload() if body is None else body
        headers = {}
        if event is not None:
            headers["X-GitHub-Event"] = event
        if delivery is not None:
            headers["X-GitHub-Delivery"] = delivery
        if signature == "auto":
            signature = sign(body)
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        if length == "auto":
            headers["Content-Length"] = str(len(body))
        elif length is not None:
            headers["Content-Length"] = str(length)
        return self.request("POST", path, body, headers)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        self.trigger.close()


@pytest.fixture
def server(store):
    rig = Rig(store)
    try:
        yield rig
    finally:
        rig.close()


CASES = {
    "no_sig": {"signature": None},
    "bad_sig": {"signature": "sha256=" + "0" * 64},
    "wrong_secret": {"signature": sign(payload(), b"other")},
    "malformed_sig": {"signature": "sha1=abc"},
    "oversize": {"body": b"x" * (MAX_BODY + 1)},
    "oversize_claimed_only": {"body": b"", "length": 10**9},
    "no_length": {"body": b"", "length": None},
    "bad_length": {"body": b"", "length": "abc"},
    "negative_length": {"body": b"", "length": "-5"},
    "unknown_event": {"event": "push"},
    "no_event": {"event": None},
    "no_delivery": {"delivery": None},
    "weird_delivery": {"delivery": "a b/../c"},
    "other_repo": {"body": payload("evil/app")},
    "bad_json": {"body": b"{nope"},
    "json_array": {"body": b"[]"},
    "wrong_path": {"path": "/other"},
}
STATUS = {
    "no_sig": 401,
    "bad_sig": 401,
    "wrong_secret": 401,
    "malformed_sig": 401,
    "oversize": 413,
    "oversize_claimed_only": 413,
    "no_length": 411,
    "bad_length": 400,
    "negative_length": 400,
    "unknown_event": 400,
    "no_event": 400,
    "no_delivery": 400,
    "weird_delivery": 400,
    "other_repo": 400,
    "bad_json": 400,
    "json_array": 400,
    "wrong_path": 404,
}


def prepared(case):
    kw = dict(CASES[case])
    body = kw.get("body")
    if case in ("bad_json", "json_array", "other_repo"):
        kw["signature"] = sign(body)  # signed, so the later checks are what reject it
    if case == "oversize":
        kw["signature"] = sign(body)
    return kw


@pytest.mark.parametrize("case", sorted(CASES))
def test_rejections(server, store, case):
    resp = server.post(**prepared(case))
    assert resp.status == STATUS[case]
    server.trigger.wait_idle(1)
    assert server.calls == 0
    assert store.meta_with_prefix("webhook:") == {}


def test_rejects_non_post_and_healthz(server):
    assert server.request("GET", "/webhook").status == 404
    assert server.request("GET", "/healthz").status == 200
    assert server.request("PUT", "/webhook").status in (404, 501)
    assert server.calls == 0


def test_valid_event_triggers_intake(server, store):
    assert server.post(delivery="d9").status == 202
    assert server.trigger.wait_idle(5)
    assert server.calls == 1
    assert "webhook:acme:d9" in store.meta_with_prefix("webhook:")


@pytest.mark.parametrize("event", sorted(ACCEPTED_EVENTS))
def test_every_accepted_event_triggers(server, event):
    assert server.post(event=event, delivery=f"d-{event}").status == 202
    assert server.trigger.wait_idle(5)
    assert server.calls == 1


def test_repo_match_is_case_insensitive(server):
    assert server.post(payload("ACME/App")).status == 202


def test_ping_is_ok_without_intake(server, store):
    assert server.post(event="ping").status == 200
    server.trigger.wait_idle(1)
    assert server.calls == 0
    assert store.meta_with_prefix("webhook:") == {}


def test_ping_still_needs_a_valid_signature(server):
    assert server.post(event="ping", signature="sha256=" + "0" * 64).status == 401


def test_duplicate_delivery_ignored(server):
    assert server.post(delivery="d1").status == 202
    assert server.post(delivery="d1").status == 200
    server.trigger.wait_idle(5)
    assert server.calls == 1


def test_forged_delivery_id_cannot_poison_dedupe(server):
    assert server.post(delivery="d1", signature="sha256=" + "0" * 64).status == 401
    assert server.post(delivery="d1").status == 202


def test_delivery_dedupe_survives_a_new_server(store, server):
    assert server.post(delivery="d1").status == 202
    server.close()
    again = Rig(store)
    try:
        assert again.post(delivery="d1").status == 200
        again.trigger.wait_idle(1)
        assert again.calls == 0
    finally:
        again.close()


def test_flood_coalesces():
    gate = threading.Event()
    started = threading.Event()
    calls = []

    def run():
        calls.append(1)
        started.set()
        gate.wait(5)

    t = IntakeTrigger(run, debounce_s=0)
    try:
        t.request()
        assert started.wait(5)
        for _ in range(50):
            t.request()
        gate.set()
        assert t.wait_idle(5)
        assert len(calls) == 2
    finally:
        gate.set()
        t.close()


def test_run_once_exception_keeps_worker_alive(capsys):
    calls = []

    def run():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    t = IntakeTrigger(run, debounce_s=0)
    try:
        t.request()
        assert t.wait_idle(5)
        t.request()
        assert t.wait_idle(5)
        assert len(calls) == 2
        assert "boom" in capsys.readouterr().err
    finally:
        t.close()


def test_close_stops_the_worker_and_ignores_later_requests():
    calls = []
    t = IntakeTrigger(lambda: calls.append(1), debounce_s=0)
    t.close()
    t.request()
    assert t.wait_idle(1)
    assert calls == []


def test_debounce_merges_a_burst():
    calls = []
    t = IntakeTrigger(lambda: calls.append(1), debounce_s=0.2)
    try:
        for _ in range(5):
            t.request()
        assert t.wait_idle(5)
        assert len(calls) == 1
    finally:
        t.close()


def test_verify_signature_format():
    body = b"hello"
    good = sign(body)
    assert verify_signature(SECRET, body, good)
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "")
    assert not verify_signature(SECRET, body, good.removeprefix("sha256="))
    assert not verify_signature(SECRET, body, good.upper())
    assert not verify_signature(SECRET, body, good + "0")
    assert not verify_signature(SECRET, body, "sha256=" + "g" * 64)
    assert not verify_signature(SECRET, b"other", good)
    assert not verify_signature(b"", body, sign(body, b""))


def test_verify_signature_uses_compare_digest(monkeypatch):
    seen = []
    real = hmac.compare_digest
    monkeypatch.setattr(
        hmac, "compare_digest", lambda a, b: (seen.append(1), real(a, b))[1]
    )
    verify_signature(SECRET, b"x", sign(b"x"))
    assert seen


def test_check_delivery_orders_signature_before_parsing():
    body = b"{not json"
    with pytest.raises(WebhookRejected) as e:
        check_delivery(
            {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d"},
            body,
            secret=SECRET,
            repo=REPO,
        )
    assert e.value.status == 401


def test_check_delivery_headers_are_case_insensitive():
    body = payload()
    d = check_delivery(
        {
            "x-hub-signature-256": sign(body),
            "x-github-event": "issues",
            "x-github-delivery": "abc",
        },
        body,
        secret=SECRET,
        repo=REPO,
    )
    assert (d.event, d.delivery_id, d.repo) == ("issues", "abc", REPO)


def test_idle_connections_cannot_exhaust_the_server(store, monkeypatch):
    monkeypatch.setattr(webhook_module, "MAX_CONNECTIONS", 1)
    rig = Rig(store)
    idle = socket.create_connection(rig.httpd.server_address)
    try:
        time.sleep(0.2)  # let the server dedicate its one slot to the idle socket
        with pytest.raises((ConnectionError, http.client.HTTPException)):
            rig.post()
        assert rig.calls == 0
        idle.close()
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline and status != 202:
            time.sleep(0.05)
            try:
                status = rig.post(delivery="after").status
            except (ConnectionError, http.client.HTTPException):
                pass
        assert status == 202
    finally:
        idle.close()
        rig.close()


def test_debounce_holds_while_requests_keep_arriving():
    calls = []
    t = IntakeTrigger(lambda: calls.append(time.monotonic()), debounce_s=0.4)
    try:
        start = time.monotonic()
        t.request()
        while time.monotonic() - start < 0.25:
            t.request()  # each one wakes the worker, which must keep waiting
            time.sleep(0.01)
        assert calls == []
        assert t.wait_idle(5)
        assert len(calls) == 1 and calls[0] - start >= 0.38
    finally:
        t.close()


def test_busy_run_is_requested_again_a_bounded_number_of_times():
    calls = []

    def busy():
        calls.append(1)
        return True  # another intake held the lease

    t = IntakeTrigger(busy, debounce_s=0, busy_retry_s=0.02, busy_retries=3)
    try:
        t.request()
        assert t.wait_idle(5)
        assert len(calls) == 4
    finally:
        t.close()


def test_busy_run_succeeds_on_a_later_attempt():
    results = iter([True, True, False])
    calls = []

    def run():
        calls.append(1)
        return next(results)

    t = IntakeTrigger(run, debounce_s=0, busy_retry_s=0.02, busy_retries=5)
    try:
        t.request()
        assert t.wait_idle(5)
        assert len(calls) == 3
    finally:
        t.close()


def test_slow_trickle_body_hits_a_total_deadline(store, monkeypatch):
    monkeypatch.setattr(webhook_module, "BODY_DEADLINE_S", 0.5)
    rig = Rig(store)
    body = payload()
    sock = socket.create_connection(rig.httpd.server_address, timeout=5)
    try:
        head = (
            "POST /webhook HTTP/1.1\r\nHost: x\r\n"
            f"Content-Length: {len(body)}\r\nX-GitHub-Event: issues\r\n"
            f"X-GitHub-Delivery: slow\r\nX-Hub-Signature-256: {sign(body)}\r\n\r\n"
        )
        sock.sendall(head.encode())
        start = time.monotonic()
        closed = False
        for byte in body:
            try:
                sock.sendall(bytes([byte]))
            except OSError:
                closed = True
                break
            time.sleep(0.05)
            if time.monotonic() - start > 3:
                break
        assert closed or sock.recv(100).startswith(b"HTTP/1.0 408")
        assert rig.calls == 0
    finally:
        sock.close()
        rig.close()
