# AI MEMORY PROTOCOL (V2 -> V4 lineage)
V2 introduced canonical files + derived FTS index + bounded context packs. V3.1/V3.1.1 added explicit session binding.
V4.1 (4.1.0) includes transactional canonical writes, per-project write locks with compare-and-swap, leases/heartbeats,
crash recovery packets, memory/physical reconciliation, structured provenance, deterministic compaction,
content-addressed artifacts, ranked local retrieval, ChatGPT bridge, health, local dashboard, verified backup/restore,
doctor V4 and an isolated selftest/stress suite. Current protocol: `AI_MEMORY_AGENT_PROTOCOL_V4.md`.
