# Why this directory is empty

Per CLAUDE.md non-negotiable #1, test fixtures here may only ever be
**recordings of real sessions** captured by the harness from the live game —
never hand-written or synthesized game data. No game server has run yet, so
there are no recordings, so this directory is empty.

When Phase 2's live check runs on the game server, real `/state` recordings
land here as `*.json` and replay-based tests can be added. Until then, the test
suite covers pure functions (which may construct their own inputs) and real
network-failure paths only.
