import json
import queue
import threading
from typing import Any, Dict, List


class EventBus:
    """
    Observer / Pub-Sub Pattern for real-time Server-Sent Events (SSE).
    Decouples domain services from HTTP streaming connections (SRP & OCP).
    """

    def __init__(self) -> None:
        self._subscribers: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def subscribe(self, token: str) -> Dict[str, Any]:
        client = {"queue": queue.Queue(maxsize=100), "token": token}
        with self._lock:
            self._subscribers.append(client)
        return client

    def unsubscribe(self, client: Dict[str, Any]) -> None:
        with self._lock:
            if client in self._subscribers:
                self._subscribers.remove(client)

    def active_tokens(self) -> set:
        with self._lock:
            return {c.get("token") for c in self._subscribers if c.get("token")}

    def publish(self, event_type: str, payload: Any) -> None:
        msg = (
            f"event: {event_type}\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        ).encode("utf-8")
        with self._lock:
            dead = []
            for sub in self._subscribers:
                try:
                    sub["queue"].put_nowait(msg)
                except Exception:
                    dead.append(sub)
            for d in dead:
                if d in self._subscribers:
                    self._subscribers.remove(d)


# Shared application event bus instance
event_bus = EventBus()
