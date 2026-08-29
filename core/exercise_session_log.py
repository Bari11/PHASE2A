"""
core/exercise_session_log.py — Unplug Mode session logging interface
═══════════════════════════════════════════════════════════════════════════
Plain data structures recording what happened during one Unplug Mode
exercise session. Deliberately just data — no dashboard, no rendering,
no persistence policy decisions. Phase 4 (the dashboard) is expected
to consume `UnplugSessionLog.to_dict()` / `to_json()`; this module
does not decide where that goes (file, DB, in-memory only) — that's a
Phase 4 concern.

One record per exercise attempt:
    exercise      → canonical id, e.g. "head_tilt"
    cv_validated  → True if computer vision judged this one; False
                    means a timed/predetermined progression was used
                    (either because the exercise is one of the two
                    CV can't reliably check — leg_raise, heel_raise —
                    or because CV was unavailable, e.g. no camera)
    result        → "correct" | "proceed_timeout" | "completed_timed"
                    ("completed_timed" = non-CV exercise that simply
                    ran its full authored animation, no verdict to give)
    duration_sec  → wall-clock time this exercise occupied
    feedback      → the last feedback line shown to the user for it
    attempts      → number of retry nudges given (0 for a clean first try
                    or for non-CV exercises)
    completed     → always True today (see ExerciseScheduler.run in
                    viewer.html — a failed/timed-out exercise still
                    counts as "completed" for session purposes; it was
                    shown, attempted, and the routine moved on). Kept
                    as an explicit field rather than inferred from
                    `result`, so Phase 4 doesn't have to re-derive it.
"""

import json
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class ExerciseAttemptLog:
    exercise: str
    label: str
    cv_validated: bool
    result: str
    duration_sec: float
    feedback: str
    attempts: int
    completed: bool = True
    timestamp: float = field(default_factory=time.time)


@dataclass
class UnplugSessionLog:
    character: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    completed: bool = False
    entries: List[ExerciseAttemptLog] = field(default_factory=list)

    def add(self, entry: ExerciseAttemptLog):
        self.entries.append(entry)

    def finish(self, completed: bool = True):
        self.ended_at = time.time()
        self.completed = completed

    @property
    def duration_sec(self) -> float:
        end = self.ended_at if self.ended_at else time.time()
        return max(end - self.started_at, 0.0)

    @property
    def cv_validated_count(self) -> int:
        return sum(1 for e in self.entries if e.cv_validated)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['duration_sec'] = self.duration_sec
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
