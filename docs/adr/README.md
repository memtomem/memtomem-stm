# Architecture Decision Records

Decisions that shape STM's contracts with the outside world. Each ADR is
small: context, decision, consequences. Statuses: Proposed / Accepted /
Superseded.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-ecosystem-integration-contracts.md) | Ecosystem integrations are per-boundary contracts, not a generic adapter | Accepted |

Per-stage contracts for the proxy pipeline itself live as comments in
`src/memtomem_stm/proxy/` (see `CLAUDE.md`) — ADRs here cover cross-tool
boundaries, not internal stage design.
