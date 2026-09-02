"""WANTED harness — the agent's brain, perception, behavior, events, overlay and clips."""

#: 0.2.0 is the first version whose `stats` rows carry LIFETIME counters rather
#: than per-session ones (totals.LIFETIME_COUNTERS_SINCE_VERSION reads this
#: boundary out of `sessions.harness_version` when it seeds the on-disk totals),
#: and the first that writes `sessions.ended_at` and `missions` rows.
__version__ = "0.2.0"
