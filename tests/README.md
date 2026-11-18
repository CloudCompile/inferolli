# tests

Hermetic — no network, no Discord, no E2B. Everything that would talk to the
outside world is patched; filesystem state goes to `tempfile.TemporaryDirectory`.

Run all:

    python3 -m unittest discover -s tests

Run one file / one test:

    python3 -m unittest tests.test_levels
    python3 -m unittest tests.test_computer.ReplyParserTests

## What's covered

| file | pins down |
|---|---|
| `test_config.py` | defaults, per-guild isolation, atomic updates, corrupt-file fallback |
| `test_memory.py` | STM window/cap/age, recall ranking, tier boundaries, rank roundtrip, LTM dedupe/caps, affinity clamps, persona opinion evolution, thought caps/pruning, decay |
| `test_ai.py` | gateway-vs-port routing, per-job models, JSON envelope parsing, 5xx fallback chain, empty-content budget retry, port-locked embed/decide |
| `test_levels.py` | movement math + clamps, confidence gates (promote ≥.65 / demote ≥.55), score bounds, board persisted in the backed-up data dir, create-then-edit, bar rendering |
| `test_models_feed.py` | diff added/removed/reprice, price formatting, **silent boot sync** (no repost after restore) |
| `test_desktop_gate.py` | token roundtrip/tamper/expiry, 401 without token, 302 to viewer, WS route gate, noVNC mounts |
| `test_tools_server.py` | reply envelope, tool hop, action stripping, admin-only computer tool, hop exhaustion, vision payload |
| `test_shepherd.py` | full pagination, client-side state filter, name/template selection, timezone-safe age math, env layering |
| `test_launcher.py` | paginated sweeps, kill-by-template/name with `keep`, failure counting |
| `test_modtools.py` | case persistence/window/cap/truncation, hierarchy + admin + bot guards, defer-safe acks |
| `test_computer.py` | **newest-first reply extraction** (the one-turn-late LANTERN bug), log password parsing, auth precedence |
| `test_backup.py` | memory archived, stale xdg sqlite excluded, S3 config checks |
| `test_rag.py` | chunk sizing/merging, html extraction, hybrid ranking + per-page cap |
| `test_abilities.py` | channel-name sanitizing, name resolution, admin-only gate |
| `test_pings.py` | role mentions, ambient opt-out |
| `test_helpers.py` | admin check, anti-storm guard, mod-log posting |
| `test_start_sh.py` | entrypoint invariants: single Xvfb, .env wait, restore-before-boot, supervisor + watchdog, opencode auth |
