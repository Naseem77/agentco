"""A small GitHub webhook receiver that feeds the existing intake (Plan 5, D9).

The receiver never acts on a payload. A delivery that is authentic, from the
configured repo and not seen before only asks for an intake run; the intake reads
GitHub itself. So a forged or replayed request can at worst cost one coalesced run,
and the first line of defence is the HMAC check, done before any parsing.
"""

import hashlib
import hmac
import json
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agentco.store.task_store import TaskStore

ACCEPTED_EVENTS = frozenset(
    {
        "issues",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "check_run",
    }
)
SOCKET_TIMEOUT_S = 10
BODY_DEADLINE_S = 30  # in total, so a slow trickle cannot hold a connection slot
BUSY_RETRY_S = 5.0
BUSY_RETRIES = 6
MAX_CONNECTIONS = 32  # slow or idle clients must not exhaust threads before auth
_SIGNATURE_RE = re.compile(r"sha256=[0-9a-f]{64}")
_DELIVERY_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


class WebhookRejected(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class Delivery:
    event: str
    delivery_id: str
    repo: str


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    if not secret or not header or not _SIGNATURE_RE.fullmatch(header):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def check_delivery(
    headers: Mapping[str, str], body: bytes, *, secret: bytes, repo: str
) -> Delivery | None:
    """Validate one delivery; `None` means a ping to acknowledge without an intake."""
    h = {k.lower(): v for k, v in headers.items()}
    if not verify_signature(secret, body, h.get("x-hub-signature-256")):
        raise WebhookRejected(401, "bad signature")
    event, delivery_id = h.get("x-github-event"), h.get("x-github-delivery")
    if not event or not delivery_id:
        raise WebhookRejected(400, "missing event or delivery header")
    if not _DELIVERY_RE.fullmatch(delivery_id):
        raise WebhookRejected(400, "malformed delivery id")
    if event == "ping":
        return None
    if event not in ACCEPTED_EVENTS:
        raise WebhookRejected(400, "event not accepted")
    try:
        data = json.loads(body)
    except ValueError as e:
        raise WebhookRejected(400, "body is not JSON") from e
    if not isinstance(data, dict):
        raise WebhookRejected(400, "body is not a JSON object")
    repository = data.get("repository")
    name = repository.get("full_name") if isinstance(repository, dict) else None
    if not isinstance(name, str) or name.lower() != repo.lower():
        raise WebhookRejected(400, "wrong repository")
    return Delivery(event, delivery_id, repo)


class IntakeTrigger:
    """Coalescing trigger: one run in progress and at most one more pending.

    Requests that arrive during the debounce window or while a run is in progress
    all collapse into a single follow-up run. A run that returns `True` found
    another intake holding the lease: it is requested again after `busy_retry_s`,
    at most `busy_retries` times in a row, since its delivery is already marked seen.
    """

    def __init__(
        self,
        run_once: Callable[[], object],
        debounce_s: float,
        *,
        busy_retry_s: float = BUSY_RETRY_S,
        busy_retries: int = BUSY_RETRIES,
    ) -> None:
        self._run_once = run_once
        self._debounce_s = debounce_s
        self._busy_retry_s = busy_retry_s
        self._busy_retries = busy_retries
        self._busy_streak = 0
        self._not_before = 0.0
        self._cond = threading.Condition()
        self._pending = False
        self._running = False
        self._closed = False
        self._worker = threading.Thread(
            target=self._work, name="agentco-intake-trigger", daemon=True
        )
        self._worker.start()

    def request(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._pending = True
            self._busy_streak = 0
            self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending or self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._pending = False
            self._cond.notify_all()
        self._worker.join(timeout=5)

    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                deadline = max(time.monotonic() + self._debounce_s, self._not_before)
                while not self._closed:  # a request also wakes this wait: keep going
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._cond.wait(left)
                if self._closed:
                    return
                self._pending = False
                self._running = True
            busy = False
            try:
                busy = self._run_once() is True
            except Exception as exc:  # noqa: BLE001 - the worker must survive any failed run
                print(f"webhook: intake run failed: {exc!r}", file=sys.stderr)
            finally:
                with self._cond:
                    self._running = False
                    if busy and self._busy_streak < self._busy_retries:
                        self._busy_streak += 1
                        self._pending = not self._closed
                        self._not_before = time.monotonic() + self._busy_retry_s
                    else:
                        self._busy_streak = 0
                    self._cond.notify_all()


def make_server(
    host: str,
    port: int,
    *,
    tenant: str,
    store: TaskStore,
    repo: str,
    secret: bytes,
    max_body: int,
    trigger: IntakeTrigger,
) -> ThreadingHTTPServer:
    dedupe = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        timeout = SOCKET_TIMEOUT_S
        server_version = "agentco-webhook"

        def log_message(self, format: str, *args: object) -> None:
            """The default access log would echo request lines; stay quiet."""

        def _reply(self, status: int, text: str) -> None:
            data = (text + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def _content_length(self) -> int:
            raw = self.headers.get("Content-Length")
            if raw is None:
                raise WebhookRejected(411, "Content-Length required")
            if not raw.isascii() or not raw.isdigit():
                raise WebhookRejected(400, "bad Content-Length")
            length = int(raw)
            if length > max_body:
                raise WebhookRejected(413, "body too large")
            return length

        def _read_body(self, length: int) -> bytes:
            deadline = time.monotonic() + BODY_DEADLINE_S
            chunks: list[bytes] = []
            got = 0
            while got < length:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise WebhookRejected(408, "body too slow")
                self.connection.settimeout(min(left, SOCKET_TIMEOUT_S))
                try:
                    chunk = self.rfile.read1(min(length - got, 65536))
                except TimeoutError as e:
                    raise WebhookRejected(408, "body too slow") from e
                if not chunk:
                    raise WebhookRejected(400, "incomplete body")
                chunks.append(chunk)
                got += len(chunk)
            return b"".join(chunks)

        def _drain(self) -> None:
            """Read a small unread body so closing does not reset the reply."""
            try:
                length = self._content_length()
                self.rfile.read(length)
            except (WebhookRejected, OSError):
                pass

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:
            try:
                self._receive()
            except WebhookRejected as e:
                self._reply(e.status, e.reason)
            except OSError:
                self.close_connection = True

        def _receive(self) -> None:
            if self.path != "/webhook":
                self._drain()
                raise WebhookRejected(404, "not found")
            body = self._read_body(self._content_length())
            delivery = check_delivery(self.headers, body, secret=secret, repo=repo)
            if delivery is None:
                self._reply(200, "pong")
                return
            key = f"webhook:{tenant}:{delivery.delivery_id}"
            with dedupe:
                seen = store.get_meta(key) is not None
                if not seen:
                    store.set_meta(key, datetime.now(UTC).isoformat())
            if seen:
                self._reply(200, "duplicate")
                return
            print(
                f"webhook: {delivery.event} {delivery.delivery_id} accepted",
                file=sys.stderr,
            )
            trigger.request()
            self._reply(202, "accepted")

    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def process_request(self, request, client_address) -> None:
            if not slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                slots.release()

        def handle_error(self, request, client_address) -> None:
            exc = sys.exc_info()[1]
            if not isinstance(exc, OSError):
                super().handle_error(request, client_address)

    return Server((host, port), Handler)
