from bridge.state import Backoff


def test_starts_at_one_second():
    b = Backoff()
    assert b.next() == 1


def test_doubles_each_call():
    b = Backoff()
    assert b.next() == 1
    assert b.next() == 2
    assert b.next() == 4
    assert b.next() == 8


def test_caps_at_60():
    b = Backoff()
    for _ in range(20):
        b.next()
    assert b.next() == 60


def test_reset_returns_to_one():
    b = Backoff()
    b.next(); b.next(); b.next()
    b.reset()
    assert b.next() == 1
