"""Participant registry: who is in the room and which language they use.

``detected_language`` is updated on every finalized utterance (latest wins —
STT language hints converge quickly). ``preferred_language`` is an explicit
declaration (data message or backend) that overrides the detected one when
deciding which language to speak TO that participant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .languages import normalize_language


@dataclass
class Participant:
    identity: str
    detected_language: str | None = None
    preferred_language: str | None = None
    utterance_count: int = 0
    detections: dict[str, int] = field(default_factory=dict)

    @property
    def listen_language(self) -> str | None:
        """Language to use when speaking TO this participant."""
        return self.preferred_language or self.detected_language

    def record_detection(self, language: str | None) -> None:
        language = normalize_language(language)
        if not language:
            return
        self.detected_language = language
        self.detections[language] = self.detections.get(language, 0) + 1
        self.utterance_count += 1

    def set_preferred(self, language: str | None) -> None:
        self.preferred_language = normalize_language(language)


class ParticipantRegistry:
    def __init__(self) -> None:
        self._participants: dict[str, Participant] = {}

    def add(self, identity: str) -> Participant:
        p = self._participants.get(identity)
        if p is None:
            p = Participant(identity=identity)
            self._participants[identity] = p
        return p

    def remove(self, identity: str) -> None:
        self._participants.pop(identity, None)

    def get(self, identity: str) -> Participant | None:
        return self._participants.get(identity)

    def require(self, identity: str) -> Participant:
        return self.add(identity)

    def identities(self) -> list[str]:
        return list(self._participants)

    def others(self, identity: str) -> list[Participant]:
        return [p for p in self._participants.values() if p.identity != identity]

    def __len__(self) -> int:
        return len(self._participants)
