"""Ack-aware loop-closure reconciliation (2026-06-22).

Root cause fixed here: both loop-closure backstops treated ANY outbound after
an inbound as "the monitor answered" -- with no content awareness.

The monitor sends a 👀 "I see you" ack via tg-send.sh on receipt; tg-send.sh
logs every send as direction=outbound (text_len=1 for the emoji). So a message
that got ONLY the ack and never a real reply still looked "answered":
  - check_stale_inbox drained the file and suppressed the wedge alert
    (_latest_outbound_epoch returned the ack ts >= the file arrival), and
  - ensure-outbound.sh (Stop hook) saw an outbound after the inbound and
    skipped the fallback.
The 👀 triple-suppressed every safety net. Observed live 2026-06-22: a personal
voice memo (update_id 514106295) got only 👀 at 22:50 PT 6/21 and was silently
dropped -- no reply, no fallback, no alert.

Fix: outbounds with text_len <= ACK_MAX_LEN do not count as answering. A real
reply is always longer than a one/two-character emoji ack.
"""
import json
from datetime import datetime

import bridge.main as main_mod


def _write_msgs(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_trailing_ack_does_not_count_as_outbound(tmp_path, monkeypatch):
    """The 295 case: inbound, then only a 👀 ack (text_len=1). No substantive
    reply ⇒ _latest_outbound_epoch returns None so the wedge alert can fire."""
    msgs = tmp_path / "msgs.jsonl"
    _write_msgs(msgs, [
        {"ts": "2026-06-22T05:50:55.493378+00:00", "direction": "inbound", "text_len": 2034},
        {"ts": "2026-06-22T05:51:06.141266400+00:00", "direction": "outbound", "text_len": 1},
    ])
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", msgs)
    assert main_mod._latest_outbound_epoch() is None


def test_returns_last_substantive_outbound_ignoring_later_ack(tmp_path, monkeypatch):
    """A real reply, then (after a new inbound) a lone ack ⇒ the reconciliation
    still points at the real reply, not the ack."""
    msgs = tmp_path / "msgs.jsonl"
    _write_msgs(msgs, [
        {"ts": "2026-06-21T02:42:50.989097+00:00", "direction": "inbound", "text_len": 326},
        {"ts": "2026-06-21T02:45:36.360622000+00:00", "direction": "outbound", "text_len": 274},
        {"ts": "2026-06-22T05:50:55.493378+00:00", "direction": "inbound", "text_len": 2034},
        {"ts": "2026-06-22T05:51:06.141266400+00:00", "direction": "outbound", "text_len": 1},
    ])
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", msgs)
    expected = datetime.fromisoformat("2026-06-21T02:45:36.360622+00:00").timestamp()
    assert main_mod._latest_outbound_epoch() == expected


def test_substantive_reply_after_ack_still_counts(tmp_path, monkeypatch):
    """Normal flow: 👀 ack then a real reply ⇒ returns the real reply ts
    (regression guard -- the common case must keep working)."""
    msgs = tmp_path / "msgs.jsonl"
    _write_msgs(msgs, [
        {"ts": "2026-06-19T20:16:08.682414+00:00", "direction": "inbound", "text_len": 44},
        {"ts": "2026-06-19T20:16:18.582082420+00:00", "direction": "outbound", "text_len": 1},
        {"ts": "2026-06-19T20:19:14.155197195+00:00", "direction": "outbound", "text_len": 2145},
    ])
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", msgs)
    expected = datetime.fromisoformat("2026-06-19T20:19:14.155197+00:00").timestamp()
    assert main_mod._latest_outbound_epoch() == expected


def test_ack_boundary_lengths(tmp_path, monkeypatch):
    """text_len <= ACK_MAX_LEN is an ack; one above the threshold is a reply."""
    msgs = tmp_path / "at.jsonl"
    _write_msgs(msgs, [
        {"ts": "2026-06-22T01:00:00.000000+00:00", "direction": "outbound",
         "text_len": main_mod.ACK_MAX_LEN},
    ])
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", msgs)
    assert main_mod._latest_outbound_epoch() is None

    msgs2 = tmp_path / "above.jsonl"
    _write_msgs(msgs2, [
        {"ts": "2026-06-22T01:00:00.000000+00:00", "direction": "outbound",
         "text_len": main_mod.ACK_MAX_LEN + 1},
    ])
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", msgs2)
    assert main_mod._latest_outbound_epoch() is not None


def test_missing_text_len_treated_as_substantive(tmp_path, monkeypatch):
    """Backward-compat: a pre-text_len outbound record must still count so the
    fix never silently swallows an older real reply."""
    msgs = tmp_path / "old.jsonl"
    _write_msgs(msgs, [
        {"ts": "2026-05-01T00:00:00.000000+00:00", "direction": "outbound"},
    ])
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", msgs)
    assert main_mod._latest_outbound_epoch() is not None
