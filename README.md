# CEDOE Template

A project template for building AI-assisted operations systems using **CEDOE** — [Compound Engineering](https://github.com/EveryInc/compound-engineering-plugin) + [DOE (Directive-Orchestration-Execution)](https://every.to/p/how-to-build-ai-agents-that-actually-work). Two well-defined systems, built by others, combined here:

- **DOE** (by [Nicholas Potts](https://x.com/nick_potts_)) separates AI decision-making from deterministic execution via structured directives
- **Compound Engineering** (by [Every](https://every.to)) adds planning, multi-agent review, and captured learnings on top

LLMs handle decision-making; deterministic Python scripts handle the work. This separation keeps things reliable at scale.

## How It Works

The system uses a 3-layer architecture:

| Layer | Role | Lives in |
|-------|------|----------|
| **Directive** | SOPs that define what to do — goals, inputs, tools, outputs, edge cases | `directives/` |
| **Orchestration** | The AI agent reads directives, calls scripts in the right order, handles errors | Your AI tool (Claude, Gemini, Codex, etc.) |
| **Execution** | Deterministic Python scripts that do the actual work — API calls, scraping, data processing | `execution/` |

**Why not let the AI do everything?** Accuracy compounds. 90% accuracy per step = 59% over 5 steps. By pushing work into deterministic scripts, the AI only needs to make good decisions — not also produce flawless code on every run.

## Quick Start

### Prerequisites

- Python 3.10+
- An AI coding tool (Claude Code, Cursor, Windsurf, etc.)

### Setup

```bash
# Clone the template
git clone <repo-url>
cd <your-project-name>

# Install base dependencies
pip install -r requirements.txt

# Add your credentials
cp .env.example .env  # Then fill in your API keys
```

### Your First Directive + Script

See [GETTING_STARTED.md](GETTING_STARTED.md) for a step-by-step walkthrough.

## Project Structure

```
.
├── directives/          # SOPs in Markdown (the instruction set)
├── execution/           # Deterministic Python scripts (the tools)
├── docs/
│   ├── plans/           # Feature implementation plans
│   ├── solutions/       # Documented learnings and fixes
│   └── brainstorms/     # Early-stage thinking
├── templates/           # Data files used by execution scripts
├── .tmp/                # Intermediate files (never committed, always regenerated)
├── .env.example         # Environment variable template
├── CLAUDE.md            # Agent instructions (mirrored across AI tools)
├── AGENTS.md            # Mirror for Codex/other agents
├── GEMINI.md            # Mirror for Gemini
├── compound-engineering.local.md  # Compound Engineering plugin config
└── requirements.txt
```

## Running a Directive

Open your AI tool in this repo and tell it what to do. The agent reads the relevant directive and orchestrates the execution scripts.

```
> Run the [your directive name] directive
```

The agent will:
1. Read the directive from `directives/`
2. Run execution scripts in the right order
3. Handle errors and self-anneal
4. Write results to your configured output (Google Sheets, etc.)
5. Report a summary

You don't write any code at runtime. The directive tells the agent what to do; the scripts do the work.

## Building New Skills (The DOE Loop)

A "skill" is a directive + its execution script(s), built as a pair:

1. **Write the directive** — define the goal, inputs, workflow steps, outputs, and edge cases in `directives/your_skill.md`
2. **Write the script** — create the execution script(s) in `execution/` that the directive references
3. **Test** — run the directive end-to-end through your AI tool
4. **Self-anneal** — when something breaks, fix the script, test again, and update the directive with what you learned

Directives and scripts evolve together. When you discover API limits, timing issues, or better approaches, both get updated. If a learning applies beyond a single directive, write it up in `docs/solutions/`.

## Compound Engineering (Optional)

[Compound Engineering](https://github.com/EveryInc/compound-engineering-plugin) adds structured planning, multi-agent code review, and captured learnings on top of the DOE loop. It's useful for complex features; overkill for simple skills.

### Installation

```bash
# In Claude Code CLI
/plugin install compound-engineering
```

After installing, run `/setup` to auto-detect your stack. Then review `compound-engineering.local.md` to make sure the 3-layer architecture context is preserved (the setup wizard may overwrite it — restore from git if needed).

> **Note:** `compound-engineering.local.md` is committed to this repo. It contains DOE-specific review context that teaches the Compound Engineering agents about the 3-layer architecture. If you re-run `/setup`, compare the generated file against git before committing.

### Workflows

| Command | What it does | When to use it |
|---------|-------------|----------------|
| `/workflows:brainstorm` | Explore an approach with structured thinking | Early-stage ideas |
| `/workflows:plan` | Research codebase, produce an implementation plan | Before building new features |
| `/workflows:work` | Execute the plan step by step | Complex multi-step features |
| `/workflows:review` | Multi-agent code review | Before merging |
| `/workflows:compound` | Record learnings from completed work | After finishing a feature |

### When to Use What

**Running an existing directive** — DOE only, no CE needed. The agent reads the directive, runs scripts, self-anneals on errors. This is 80% of daily usage.

**Building a new simple skill** (one directive, one script) — DOE + CE at the end:
1. Write the directive and script (DOE loop: write, test, self-anneal)
2. `/workflows:review` to catch issues
3. `/workflows:compound` to capture learnings

**Building something complex** (multiple scripts, new integrations, open questions) — full CE cycle:
1. `/workflows:brainstorm` to explore the approach
2. `/workflows:plan` to design the implementation
3. Build out the plan
4. `/workflows:review` for multi-agent review
5. `/workflows:compound` to capture learnings

**The relationship:** DOE is the engine (how every directive runs). CE is the workshop (how you build and improve the engine). You always use DOE. You use CE when building or evolving.

### Configuration

Review agents and project context are configured in `compound-engineering.local.md`. The default agents are:

- **code-simplicity-reviewer** — flags unnecessary complexity
- **security-sentinel** — checks for security issues
- **performance-oracle** — checks for performance problems

Edit the YAML frontmatter to add, remove, or swap agents.

## Agent Instructions

Agent instructions live in `CLAUDE.md` (mirrored to `AGENTS.md` and `GEMINI.md`). These files teach the AI agent how to operate within the 3-layer architecture — they're the framework's operating manual. Changes to one must be mirrored to all three.

The instructions include execution script standards (structured output, security patterns, performance guidelines) learned from real-world usage. See `CLAUDE.md` for the full reference.

## Contributing

- **Mirror agent instructions** — changes to `CLAUDE.md` must be mirrored to `AGENTS.md` and `GEMINI.md`
- **Deliverables go to cloud services** — Google Sheets, Slides, etc. Local files are for processing only
- **`.tmp/` is ephemeral** — everything in it can be deleted and regenerated
- **Update directives as you learn** — directives are living documents, not write-once specs
- **Don't commit secrets** — `.env`, `credentials.json`, and `token.json` are in `.gitignore`
