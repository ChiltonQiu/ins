"""The clock.

It asks the question; send.py answers it. What is tested here is that it keeps
asking — a clock that dies of one bad night's data is silent every day after,
which is the bug this whole feature exists to fix.
"""

import dataclasses
import threading

import pytest
from sqlalchemy.orm import sessionmaker

from renewal.digest.clock import DigestClock
from renewal.ingest import ingest_pdf
from renewal.models import NotificationSend
from renewal.settings_store import write
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings


class _Stop(Exception):
    pass


class _StoppingClock(DigestClock):
    """A clock whose loop ends when the injected sleep says so.

    run() is an infinite loop in production and should stay one. Swallowing
    the stop here rather than teaching the clock a stopping condition keeps a
    test's needs out of the thing being tested — and keeps the daemon test
    from dying of an unhandled exception, which pytest reports as a warning
    and which looks exactly like the failure the test is meant to rule out.
    """

    def run(self) -> None:
        try:
            super().run()
        except _Stop:
            pass


def _configured(**overrides):
    base = {
        "smtp_host": "smtp.example.com",
        "notify_to": "her@agency.com",
        "notify_from": "app@agency.com",
        "agency_tz": "UTC",
        "digest_hour": 0,
    }
    return dataclasses.replace(_settings(), **(base | overrides))


def _clock(engine, sent, cls=DigestClock, **kwargs):
    return cls(
        sessionmaker(bind=engine), _configured(),
        send=lambda subject, body, **k: sent.append(subject), **kwargs,
    )


def _arrived(engine, store):
    with sessionmaker(bind=engine)() as session:
        ingest_pdf(
            session, store, data=make_text_pdf([["unrecognisable"]]),
            original_filename="a.pdf", source="manual_upload", agency_id=1,
        )
        session.commit()


def test_a_tick_sends_and_commits(engine, clean_db, store):
    """It commits: a summary whose row is rolled back is a summary that goes
    out again five minutes later, and every five minutes after that."""
    _arrived(engine, store)

    sent = []
    assert _clock(engine, sent).tick()
    assert len(sent) == 1

    with sessionmaker(bind=engine)() as session:
        assert session.query(NotificationSend).count() == 1

    # And the row it committed is what stops the next tick.
    assert not _clock(engine, sent).tick()
    assert len(sent) == 1


def test_a_tick_reads_her_settings_not_the_environment(engine, clean_db, store):
    """digest_enabled is a preference. Turning it off on /settings has to
    reach the thread, which never sees a request."""
    _arrived(engine, store)
    with sessionmaker(bind=engine)() as session:
        write(session, "digest_enabled", "")
        session.commit()

    sent = []
    assert not _clock(engine, sent).tick()
    assert sent == []


def test_the_loop_keeps_going_after_a_tick_raises(engine, clean_db):
    """One bad night's data must not end the clock."""
    ticks = []

    def boom():
        ticks.append(1)
        raise RuntimeError("the database went away")

    def sleeper(seconds):
        # Ends the loop once three ticks have been survived. Raising out of
        # sleep rather than out of tick is what makes this a test about the
        # loop's tolerance rather than about its stopping condition.
        if len(ticks) >= 3:
            raise _Stop

    clock = _clock(engine, [], tick_seconds=0, sleep=sleeper)
    clock.tick = boom

    with pytest.raises(_Stop):
        clock.run()
    assert len(ticks) == 3


def test_the_thread_is_a_daemon(engine, clean_db):
    """It must never hold a shutdown open. Work in this process dies with this
    process, the same contract background.py states."""
    started = threading.Event()

    def sleeper(seconds):
        started.set()
        raise _Stop

    clock = _clock(engine, [], cls=_StoppingClock, tick_seconds=0,
                   sleep=sleeper)
    thread = clock.start()
    assert thread.daemon
    assert started.wait(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()
