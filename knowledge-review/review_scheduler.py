"""FSRS adapter. Replay original times; disable fuzz so corrections are reproducible."""

from datetime import datetime, timezone
from fsrs import Card, Rating, Scheduler

SCHEDULER_VERSION = "fsrs-6.3.2"


def initial_schedule(timestamp: str) -> tuple[dict, dict]:
    scheduler = Scheduler(enable_fuzzing=False)
    card = Card(due=datetime.fromisoformat(timestamp))
    return card.to_dict(), scheduler.to_dict()


def replay(initial_card: dict, parameters: dict, events: list[dict]) -> dict:
    scheduler = Scheduler.from_dict(parameters)
    card = Card.from_dict(initial_card)
    for event in events:
        if event["effective_rating"] is not None:
            card, _ = scheduler.review_card(
                card, Rating[event["effective_rating"]],
                review_datetime=datetime.fromisoformat(event["reviewed_at"]),
            )
    return card.to_dict()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
