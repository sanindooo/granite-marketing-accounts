---
review_agents: [code-simplicity-reviewer, security-sentinel, performance-oracle]
plan_review_agents: [code-simplicity-reviewer]
---

# Review Context

## Architecture: 3-Layer System

This project uses a directive/orchestration/execution architecture. The LLM (Layer 2) orchestrates between human-written SOPs (Layer 1) and deterministic Python scripts (Layer 3). This separation exists because LLMs are probabilistic and business logic requires consistency.

- `directives/` — SOPs in Markdown. Before building anything, check if a directive already exists. Directives define goals, inputs, tools/scripts to use, outputs, and edge cases.
- `execution/` — Deterministic Python scripts. These handle API calls, data processing, file operations, database interactions. Always prefer calling an existing script over writing inline logic.
- `.tmp/` — Intermediate files only. Never commit. Always regenerated.
- `.env` — Environment variables and API keys.
- Deliverables go to cloud services (Google Sheets, Slides), not local files.

## Rules for Plans

When creating plans in `docs/plans/`:
- Always list which existing directives in `directives/` are relevant to the work
- Always list which existing execution scripts in `execution/` will be used or need to be created
- If a directive doesn't exist for the workflow being planned, note that one needs to be created as part of the plan
- Reference the self-annealing principle: new scripts should be tested and directives updated with learnings
- Check `docs/solutions/` for past learnings that apply to the current plan

## Rules for Solutions

When writing solutions in `docs/solutions/`:
- If the learning affects an operational workflow, ALSO update the relevant directive in `directives/`
- Tag solutions with which directives and scripts they relate to
- Solutions capture the "why" and the broader pattern; directives capture the "how"
- Include enough context that the learnings-researcher agent can surface this solution for similar future issues

## Rules for Reviews

- Check that new code follows the 3-layer separation (no API calls in orchestration logic—those belong in `execution/` scripts)
- Verify that new scripts have corresponding directive references
- Flag any hardcoded credentials (should be in `.env`)
- Check that deliverables target cloud services, not local files
- Ensure `.tmp/` is used for any intermediate file output
