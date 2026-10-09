"""What Resolv remembers about this one PC. Everything stays in a small JSON file on this computer.

Two things are kept:
  baseline - a picture of the network settings the last time the internet was working.
             It lets Resolv answer "what changed since it last worked?".
  history  - past problems on this PC: the cause, the fix, and whether it worked.
             Causes seen before are checked first next time, so repeat problems are found faster.

Nothing here is model training. It is memory the agent reads before it starts.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter

from .rules import CAUSES, KEY_CHECK

MAX_HISTORY = 200


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


class Memory:
    def __init__(self, path: str | None):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"baseline": None, "history": []}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data["baseline"] = loaded.get("baseline") or None
                    self.data["history"] = list(loaded.get("history") or [])
            except (OSError, ValueError):
                pass  # a damaged memory file must never stop Resolv from starting

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ------------------------------------------------------------ baseline
    @property
    def baseline(self):
        return self.data["baseline"]

    def set_baseline(self, snapshot: dict):
        with self.lock:
            self.data["baseline"] = {"snapshot": snapshot, "saved_at": now_text()}
            self._save()

    # ------------------------------------------------------------ history
    def add_incident(self, question: str, cause: str, fix_id: str | None, fixed, seconds: float, checks: int, brain: str):
        with self.lock:
            self.data["history"].append(
                {
                    "when": now_text(),
                    "question": question[:200],
                    "cause": cause,
                    "fix": fix_id,
                    "fixed": fixed,  # True, False, or None when there was nothing to apply
                    "seconds": round(seconds, 1),
                    "checks": checks,
                    "brain": brain,
                }
            )
            self.data["history"] = self.data["history"][-MAX_HISTORY:]
            self._save()

    def mark_last(self, cause: str, fixed):
        with self.lock:
            for h in reversed(self.data["history"]):
                if h["cause"] == cause:
                    h["fixed"] = fixed
                    break
            self._save()

    def seen(self, cause: str) -> dict:
        """How often this cause came up before, and when it was last fixed."""
        past = [h for h in self.data["history"] if h["cause"] == cause]
        fixed = [h for h in past if h.get("fixed")]
        return {"times": len(past), "last": past[-1]["when"] if past else None, "times_fixed": len(fixed)}

    def real_causes(self) -> Counter:
        skip = {"no_fault_found", "pc_looks_healthy"}
        return Counter(h["cause"] for h in self.data["history"] if h["cause"] in CAUSES and h["cause"] not in skip)

    def preferred_checks(self) -> list:
        """Checks that found the cause on this PC before, most frequent first."""
        out = []
        for cause, _n in self.real_causes().most_common():
            chk = KEY_CHECK.get(cause)
            if chk and chk not in out:
                out.append(chk)
        return out

    def history_note(self) -> str:
        """One line the language model reads before it starts."""
        counts = self.real_causes()
        if not counts:
            return ""
        parts = [f"{CAUSES[c][0].lower()} ({n}x)" for c, n in counts.most_common(3)]
        return "Problems found on this PC before: " + "; ".join(parts) + "."

    def clear(self):
        with self.lock:
            self.data = {"baseline": None, "history": []}
            self._save()

    def public(self) -> dict:
        b = self.data["baseline"]
        return {
            "has_baseline": bool(b),
            "baseline_saved_at": b["saved_at"] if b else None,
            "incidents": len(self.data["history"]),
            "recent": [
                {**h, "title": CAUSES.get(h["cause"], (h["cause"],))[0]} for h in reversed(self.data["history"][-6:])
            ],
        }

    def ctx(self) -> dict:
        return {"baseline": self.data["baseline"]}
