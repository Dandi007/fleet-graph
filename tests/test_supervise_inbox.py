"""The pending-verdict view, against a routed fake bus.

Message shapes are the ones read off the live board (real GETs, 2026-08-27):
notes carry `created_at`, refs come back as `{"refs": [{"message_id": ...}]}`.
"""

from __future__ import annotations

from typing import Any

from fleet_graph.bus.client import BusClient
from fleet_graph.supervise.inbox import format_age, list_pending, render_text

NOW = 1_700_000_000.0


class RoutedTransport:
    """Answers by URL, so one test can hold a whole board in its head."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        assert method == "GET", f"inbox must stay read-only, saw {method} {url}"
        self.calls.append(url)
        for fragment, body in self.routes.items():
            if fragment in url:
                return 200, body
        return 200, {"messages": [], "head_seq": 0, "refs": []}


def note(message_id: str, seq: int, note_type: str, card: str, text: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": "work.note.v1",
        "entity_id": message_id,
        "payload": {"card_entity_id": card, "note": text, "note_type": note_type},
        "created_at": "2023-11-14T22:13:20Z",  # NOW exactly
    }


def decision(message_id: str, seq: int, card: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel_seq": seq,
        "kind": "work.decision.v1",
        "entity_id": message_id,
        "payload": {"card_entity_id": card, "decision": "go", "decided_by": "human:test"},
        "created_at": "2023-11-14T22:13:20Z",
    }


def card(entity: str, seq: int, **payload: Any) -> dict[str, Any]:
    return {
        "message_id": f"{entity}-rev{seq}",
        "channel_seq": seq,
        "kind": "work.card.v1",
        "entity_id": entity,
        "payload": payload,
        "created_at": "2023-11-14T22:13:20Z",
    }


def client_for(
    notes: list[dict[str, Any]], cards: list[dict[str, Any]], refs: dict[str, list[str]]
):
    routes: dict[str, Any] = {
        "board:work-notes/messages": {
            "messages": notes,
            "head_seq": notes[-1]["channel_seq"] if notes else 0,
        },
        "board:work-index/messages": {
            "messages": cards,
            "head_seq": cards[-1]["channel_seq"] if cards else 0,
        },
    }
    for entity, message_ids in refs.items():
        routes[f"/v1/entities/{entity}/refs"] = {
            "refs": [{"message_id": mid, "target_entity": entity} for mid in message_ids]
        }
    transport = RoutedTransport(routes)
    return BusClient(token="t", transport=transport), transport


def test_question_without_decision_is_pending() -> None:
    notes = [note("q1", 1, "question", "card-a", "需要拍板：能不能合\n更多细节")]
    cards = [card("card-a", 1, title="线A", status="doing", work_folder_id="wf-000001")]
    client, _ = client_for(notes, cards, refs={"q1": []})

    rows = list_pending(client, now=NOW)

    assert [r.question_note_id for r in rows] == ["q1"]
    row = rows[0]
    assert row.summary == "需要拍板：能不能合"
    assert row.work_folder_id == "wf-000001"
    assert row.card_status == "doing"
    assert row.age_seconds == 0
    assert row.has_evidence_followup is False


def test_question_with_decision_ref_is_not_pending() -> None:
    notes = [
        note("q1", 1, "question", "card-a", "需要拍板"),
        decision("d1", 2, "card-a"),
    ]
    client, _ = client_for(notes, [], refs={"q1": ["d1"]})

    assert list_pending(client, now=NOW) == []


def test_evidence_followup_is_flagged_but_still_pending() -> None:
    notes = [
        note("q1", 1, "question", "card-a", "需要拍板"),
        note("e1", 2, "evidence", "card-a", "审计报告"),
    ]
    client, _ = client_for(notes, [], refs={"q1": ["e1"]})

    rows = list_pending(client, now=NOW)

    assert len(rows) == 1
    assert rows[0].has_evidence_followup is True


def test_non_question_notes_are_ignored() -> None:
    notes = [
        note("p1", 1, "progress", "card-a", "推进中"),
        note("f1", 2, "finding", "card-a", "发现"),
    ]
    client, transport = client_for(notes, [], refs={})

    assert list_pending(client, now=NOW) == []
    # No refs lookups for notes that are not questions.
    assert not [url for url in transport.calls if "/refs" in url]


def test_truncated_fetch_refuses_instead_of_rendering_a_gap() -> None:
    notes = [note("q1", 1, "question", "card-a", "问")]
    routes = {
        "board:work-notes/messages": {"messages": notes, "head_seq": 5000},
    }
    client = BusClient(token="t", transport=RoutedTransport(routes))

    import pytest

    with pytest.raises(RuntimeError, match="silent gap"):
        list_pending(client, now=NOW)


def test_render_text_and_age_formatting() -> None:
    assert format_age(0) == "0m"
    assert format_age(3 * 3600 + 40 * 60) == "3h40m"
    assert format_age(26 * 86400 + 3 * 3600) == "26d3h"

    notes = [note("q1", 1, "question", "card-a", "需要拍板")]
    client, _ = client_for(notes, [], refs={"q1": []})
    text = render_text(list_pending(client, now=NOW))
    assert "q1" in text
    assert "无审计跟帖" in text
