"""A recall's budget ledger (2.6).

Every bounded quantity a recall spends is declared when it starts and counted as
it is spent: how many times the ordinary arms fetch, how many times the block
arm fetches, how many stages the time cue's loop runs, and how many hypotheses
the recall evaluates. The limits are the bounds the code already held -- one
ordinary fetch, one block fetch, at most two cue stages -- written in one place,
so a stage that would spend more has to be declared here first, and a traced
recall says what it was allowed and what it used.

Retrieval is counted per arm and never summed: a cue stage is not an ordinary
fetch, and the block arm's search is neither.

An iteration evaluates one hypothesis on the rows already held; it fetches
nothing, so iterations cannot spend retrieval. The iteration budget is how many
hypotheses a recall may evaluate. Today there is one -- the order the recall's
stages produce -- so a recall evaluates it and stops, and the budget beyond one
buys nothing yet: the stop reason says so (`no_new_hypothesis`) rather than
leaving a larger budget to look as though it was used.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

ORDINARY_FETCH = "ordinary_fetch"
BLOCK_FETCH = "block_fetch"
CUE_STAGE = "cue_stage"
ITERATION = "iteration"

# The ordinary arms fetch once; the block arm fetches once; the cue loop runs its
# first stage and at most one wider one.
ORDINARY_FETCHES = 1
BLOCK_FETCHES = 1
CUE_STAGES = 2

# The iteration budget a recall runs with when its caller names none.
DEFAULT_ITERATIONS = 1

# Why the iterations stopped.
STOP_BUDGET = "budget_exhausted"
STOP_NO_HYPOTHESIS = "no_new_hypothesis"


class BudgetExceeded(RuntimeError):
    """A stage spent past its declared limit: a defect in the stage, not a runtime condition."""


@dataclass
class Ledger:
    limits: Mapping[str, int]
    used: dict[str, int] = field(default_factory=dict)

    @classmethod
    def for_recall(cls, iterations: int | None = None) -> Ledger:
        budget = DEFAULT_ITERATIONS if iterations is None else int(iterations)
        if budget < 1:
            raise ValueError(f"an iteration budget is at least 1 (the recall's own order); got {budget}")
        return cls(
            MappingProxyType(
                {
                    ORDINARY_FETCH: ORDINARY_FETCHES,
                    BLOCK_FETCH: BLOCK_FETCHES,
                    CUE_STAGE: CUE_STAGES,
                    ITERATION: budget,
                }
            )
        )

    def allows(self, kind: str) -> bool:
        """Whether one more `kind` fits the limit."""
        return self.used.get(kind, 0) < self.limits[kind]

    def spend(self, kind: str) -> None:
        """Count one `kind`; spending past the limit is refused, not recorded."""
        if not self.allows(kind):
            raise BudgetExceeded(f"{kind}: the limit is {self.limits[kind]}")
        self.used[kind] = self.used.get(kind, 0) + 1

    def iteration_stop(self) -> str:
        """Why the iterations stopped, once the recall's hypotheses are evaluated."""
        return STOP_NO_HYPOTHESIS if self.allows(ITERATION) else STOP_BUDGET

    def report(self) -> dict:
        """The limits, what was used of each, and why the iterations stopped."""
        return {
            "limits": dict(self.limits),
            "used": {kind: self.used.get(kind, 0) for kind in self.limits},
            "iterations": {
                "requested": self.limits[ITERATION],
                "evaluated": self.used.get(ITERATION, 0),
                "stop": self.iteration_stop(),
            },
        }
