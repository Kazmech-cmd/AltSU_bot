from altgu_bot.sessions import LoginThrottle, SessionStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------- SessionStore ----------

def test_session_is_returned_before_ttl():
    clock = FakeClock()
    store = SessionStore(ttl=60, clock=clock)
    store.set(1, [{"name": "sid", "value": "x"}])
    clock.now += 59
    assert store.get(1) == [{"name": "sid", "value": "x"}]


def test_session_expires_after_ttl():
    clock = FakeClock()
    store = SessionStore(ttl=60, clock=clock)
    store.set(1, [{"name": "sid", "value": "x"}])
    clock.now += 60
    assert store.get(1) is None


def test_sessions_are_per_user():
    store = SessionStore(ttl=60, clock=FakeClock())
    store.set(1, [{"name": "a", "value": "1"}])
    assert store.get(2) is None


def test_drop_removes_session():
    store = SessionStore(ttl=60, clock=FakeClock())
    store.set(1, [])
    store.drop(1)
    assert store.get(1) is None
    store.drop(1)  # повторный drop не падает


# ---------- LoginThrottle ----------

def test_throttle_blocks_after_max_failures():
    throttle = LoginThrottle(max_attempts=3, window=600, clock=FakeClock())
    for _ in range(2):
        throttle.register_failure(1)
    assert throttle.allowed(1)
    throttle.register_failure(1)
    assert not throttle.allowed(1)


def test_throttle_is_per_user():
    throttle = LoginThrottle(max_attempts=1, window=600, clock=FakeClock())
    throttle.register_failure(1)
    assert not throttle.allowed(1)
    assert throttle.allowed(2)


def test_throttle_unblocks_after_window():
    clock = FakeClock()
    throttle = LoginThrottle(max_attempts=2, window=600, clock=clock)
    throttle.register_failure(1)
    throttle.register_failure(1)
    assert not throttle.allowed(1)
    clock.now += 601
    assert throttle.allowed(1)


def test_throttle_reset_after_success():
    throttle = LoginThrottle(max_attempts=2, window=600, clock=FakeClock())
    throttle.register_failure(1)
    throttle.register_failure(1)
    throttle.reset(1)
    assert throttle.allowed(1)


def test_throttle_retry_after():
    clock = FakeClock()
    throttle = LoginThrottle(max_attempts=1, window=600, clock=clock)
    assert throttle.retry_after(1) == 0
    throttle.register_failure(1)
    clock.now += 100
    assert 495 <= throttle.retry_after(1) <= 502
