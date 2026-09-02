"""Shared test doubles that must NOT be doubles.

`LifetimeTotals` is the store the public counters now live in, so a test that
stubs it out stops testing the thing that broke. Every test here therefore gets
a REAL `LifetimeTotals` writing to a real throwaway directory; only the
directory is disposable.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from wasted_harness.totals import TOTALS_FILENAME, LifetimeTotals


def throwaway_totals(**seed: float) -> LifetimeTotals:
    """A real LifetimeTotals in a fresh temp dir, optionally pre-loaded.

    `seed` takes the same counter names the class carries, so a test can start
    from "this rig already has 3 deaths on the board" — which is exactly the
    state a watchdog restart lands in.
    """
    totals = LifetimeTotals(path=Path(tempfile.mkdtemp(prefix="wasted-totals-")) / TOTALS_FILENAME)
    for key, value in seed.items():
        setattr(totals, key, value)
    return totals
