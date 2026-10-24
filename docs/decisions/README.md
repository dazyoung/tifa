# Decisions

Architecture decision records for Tifa: one file per decision, `ADR-NNNN-short-title.md`, numbered in order and
never renumbered. Each starts with its status (Proposed, Accepted, or Superseded by ADR-NNNN), its date and what it
builds on, then the context, the decision, its consequences and the alternatives considered. A decision is changed
by a new record, not by rewriting an accepted one.

Tifa's first design (recording TfL line status every minute, the REST API and the MCP server) predates this folder;
it is described in [contracts.md](../contracts.md), [data-model.md](../data-model.md) and
[operations.md](../operations.md).

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](ADR-0001-national-rail-operator-disruptions.md) | Record National Rail operator disruptions from National Rail Enquiries' Knowledgebase feeds | Proposed |
