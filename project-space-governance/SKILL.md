---
name: project-space-governance
description: Initialize and maintain a Markdown project workspace that enables reliable AI agent handoffs and gives business stakeholders a clear project overview. Use when setting up project documentation, resuming an unfamiliar repository, updating project state at the end of a work session, recording durable rules or decisions, or creating a business-readable project brief.
---

# Project Space Governance

Create a small, auditable project context that does not depend on one conversation or one agent.

## Inspect First

1. Read `AGENTS.md` and existing project documentation before creating or changing files.
2. Preserve existing project conventions. Do not overwrite a populated document; merge only confirmed, non-duplicated information.
3. Treat code, tests, and deployment configuration as the authority for actual runtime behavior. Do not restate them in project-management files.

## Use These Document Boundaries

| File | Purpose | Reader |
| --- | --- | --- |
| `PROJECT.md` | Purpose, scope, current business framing, and links to the authoritative project materials | Business stakeholders and new agents |
| `WORKLOG.md` | Current objective, verified results, blockers, next action, and concise dated records | Agent taking over work |
| `MEMORY.md` | Stable rules, domain invariants, and collaboration conventions that remain useful across tasks | Agents |
| `docs/adr/NNN-title.md` | Important adopted decision, rationale, and consequence | Agents and maintainers |
| `topics/` | Confirmed conclusions for discrete business or project discussions | Business stakeholders and agents |

Keep one authority per fact. Reference another file instead of copying its body.

## Initialize a Project

1. Create only the missing files from `assets/templates/`.
2. Fill them with confirmed facts from existing artifacts. Mark unknown information as unknown; do not infer it.
3. Add an ADR only for decisions that affect future work or several components.
4. Add an entry to the project's existing index if it has one.
5. Explain the resulting files and identify `PROJECT.md` as the business reading entry.

## Resume Work

1. Read `PROJECT.md`, `WORKLOG.md`, and `MEMORY.md`, if present.
2. Read only the ADRs and topic documents relevant to the requested work.
3. Verify the current state against source code, tests, or the task tracker before acting when it matters.
4. Treat a user request as current direction. Record it as a confirmed project fact only after it is explicit or accepted.

## Close a Work Session

1. Refresh the `WORKLOG.md` current-state panel first.
2. Add only the session's completed work, decisions, verification evidence, unresolved blockers, and a directly executable next action.
3. Move durable rules to `MEMORY.md` and durable decisions to an ADR. Do not duplicate them in the log.
4. Never write secrets, credentials, tokens, passwords, or personal data. State only that a required secret or environment variable is configured.
5. For concurrent agents, record task ownership and modified areas before editing shared state files.

## Validate

Confirm that the business reader can understand the project by reading only `PROJECT.md` and its linked topic documents. Confirm that a new agent can identify the next action by reading `WORKLOG.md` without replaying conversation history.

