"""Standing event triggers: "when X happens, do Y" — taught once, evaluated
on every new event, and still gated every time they fire."""

from .store import Routine, Routines, trigger_matches

__all__ = ["Routine", "Routines", "trigger_matches"]
