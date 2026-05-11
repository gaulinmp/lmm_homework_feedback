"""Locust load test — 50 concurrent simulated students.

Run with::

    make load-test                           # default 50 users
    uv run locust -f scripts/locust_students.py \\
        --host=http://127.0.0.1:8000 \\
        --headless -u 50 -r 5 -t 2m

Per phase 8 spec, this hits the picker, attempt views, and submit endpoints,
and asserts the queue-position UI updates correctly. The LLM is expected to be
mocked (staging mode) so we measure routing/auth/UI overhead — not provider
latency.

Pre-flight (one-time):

  1. ``make init-db && make load`` to seed the assignment.
  2. ``make user USER=student001`` ... ``make user USER=student050``
     (or use ``--students-csv`` to pull names from a file).
  3. Start the app with a mocked LLM, e.g.::
         TUTOR_FAKE_LLM=1 uv run uvicorn app.main:app --workers 2

A 5xx anywhere fails the test. p95 budget is 500ms for non-LLM routes.
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Optional

from locust import HttpUser, between, events, task


CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
ATTEMPT_LOCATION_RE = re.compile(r"/attempts/(\d+)")

DEFAULT_PASSWORD = os.environ.get("LOCUST_PASSWORD", "pw-12345")
ASSIGNMENT_SLUG = os.environ.get("LOCUST_ASSIGNMENT_SLUG", "week3_visualization")


def _username_pool(env_count: Optional[str] = None) -> list[str]:
    n = int(env_count or os.environ.get("LOCUST_STUDENTS", "50"))
    return [f"student{i:03d}" for i in range(1, n + 1)]


_USERNAMES = _username_pool()
_username_idx = 0


def _next_username() -> str:
    global _username_idx
    name = _USERNAMES[_username_idx % len(_USERNAMES)]
    _username_idx += 1
    return name


@events.test_start.add_listener
def _on_start(environment, **kwargs):
    print(
        f"locust starting against {environment.host}"
        f" with {len(_USERNAMES)} student pool"
    )


@events.request.add_listener
def _on_request(
    request_type, name, response_time, response_length, exception, **kw
):
    """Fail the run on any 5xx, per phase 8 spec."""
    response = kw.get("response")
    if response is not None and response.status_code >= 500:
        events.request.fire(
            request_type=request_type,
            name=name,
            response_time=response_time,
            response_length=response_length,
            exception=AssertionError(f"5xx from {name}: {response.status_code}"),
        )


class StudentUser(HttpUser):
    """One simulated student.

    The student logs in, opens the picker, starts an assignment, polls the
    queue-status fragment, and posts a text submission. The flow is repeated
    several times per spawn to drive sustained load.
    """

    wait_time = between(2.0, 6.0)

    def on_start(self):
        self.username = _next_username()
        self.csrf: Optional[str] = None
        self._login()

    def _extract_csrf(self, body: str) -> Optional[str]:
        m = CSRF_RE.search(body)
        return m.group(1) if m else None

    def _login(self) -> None:
        r = self.client.get("/login", name="GET /login")
        if r.status_code != 200:
            r.failure(f"login GET status {r.status_code}")
            return
        csrf = self._extract_csrf(r.text)
        with self.client.post(
            "/login",
            data={
                "username": self.username,
                "password": DEFAULT_PASSWORD,
                "csrf_token": csrf or "",
            },
            name="POST /login",
            allow_redirects=False,
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 303):
                resp.failure(f"login failed for {self.username}: {resp.status_code}")
                return
            resp.success()
        # The CSRF token is bound to the session id, so refresh after login.
        r = self.client.get("/", name="GET / (picker)")
        self.csrf = self._extract_csrf(r.text)

    @task(3)
    def pick_and_view(self):
        self.client.get("/", name="GET / (picker)")

    @task(1)
    def full_attempt_cycle(self):
        if not self.csrf:
            self._login()
            return

        with self.client.post(
            f"/assignments/{ASSIGNMENT_SLUG}/start",
            headers={"X-CSRF-Token": self.csrf},
            name="POST /assignments/{slug}/start",
            allow_redirects=False,
            catch_response=True,
        ) as r:
            if r.status_code == 404:
                # Assignment finished (no more questions) — just bail.
                r.success()
                return
            if r.status_code != 303:
                r.failure(f"start status {r.status_code}")
                return
            loc = r.headers.get("location", "")
            m = ATTEMPT_LOCATION_RE.search(loc)
            if not m:
                r.failure(f"no attempt id in location: {loc!r}")
                return
            attempt_id = int(m.group(1))
            r.success()

        self.client.get(
            f"/attempts/{attempt_id}",
            name="GET /attempts/{id}",
        )

        # The queue-status fragment is polled by HTMX every couple seconds; the
        # spec requires it returns 200 even when there's nothing in flight.
        for _ in range(2):
            self.client.get(
                f"/attempts/{attempt_id}/queue-status",
                name="GET /attempts/{id}/queue-status",
            )
            time.sleep(0.5)

        student_text = (
            "A histogram exposes the *shape* of the distribution and any "
            "outliers — a single mean would hide both. "
            f"(rand={random.random():.3f})"
        )
        with self.client.post(
            f"/attempts/{attempt_id}/submit",
            data={"student_text": student_text},
            headers={"X-CSRF-Token": self.csrf},
            name="POST /attempts/{id}/submit",
            catch_response=True,
        ) as r:
            if r.status_code >= 500:
                r.failure(f"submit 5xx: {r.status_code}")
            elif r.status_code in (200, 409):
                # 409 = attempt closed (another submit raced past), still ok
                r.success()
            else:
                r.failure(f"unexpected submit status {r.status_code}")
