# The agent's mission knowledge base

Data extracted 2026-09-02 by a research workflow from public GTA V walkthrough wikis (the
gta.wiki mirror of GTA Wiki, cross-checked against gtabase.com; gta.fandom.com answered HTTP 402
to the fetcher), reviewed by an Opus QA pass whose corrections are recorded in `missions.json`
`qa` and in each mission's `notes`. Objectives and tips are paraphrased, not copied.

- `missions.json` — story missions in order: objectives, fail conditions, tips, crew, vehicles.
  Loaded by `brain/mission_knowledge.py`; ONE mission's bounded card is injected into the dynamic
  context while that mission is active. Never part of the cached prompt prefix.
- `hud_legend_fetched.md` / `mechanics.md` / `prompts.md` — reference material. The legend the
  brain actually carries is `brain/prompts/hud_legend.md` (translated into /state fields).
