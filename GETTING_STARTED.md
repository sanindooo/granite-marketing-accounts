# Getting Started

This guide walks you through creating your first directive + execution script pair using the DOE (Directive-Orchestration-Execution) framework.

## What You'll Build

A simple skill that fetches weather data for a city and saves it to a JSON file. This demonstrates the core DOE pattern: a directive tells the AI agent what to do, and an execution script does the deterministic work.

## Step 1: Write the Directive

Create `directives/check_weather.md`:

```markdown
# Directive: Check Weather

## Goal
Fetch current weather data for a given city and save it to `.tmp/`.

## When to Use
When the user asks for current weather information for a city.

## Inputs
- City name (provided by user)

## Outputs
- `.tmp/weather_result.json` — weather data for the requested city

## Workflow

### Step 1: Fetch Weather
```bash
python execution/fetch_weather.py --city "City Name" --output .tmp/weather_result.json
```

### Step 2: Report
Read `.tmp/weather_result.json` and present a summary to the user.

## Edge Cases

| Situation | Handling |
|-----------|----------|
| City not found | Script returns error JSON, report to user |
| API rate limit | Script returns error JSON with `rate_limited` code |
```

**Key points:**
- The directive defines the *what*, not the *how* — it tells the agent which script to run and what inputs/outputs to expect
- Edge cases are documented so the agent knows how to handle errors
- All intermediate files go in `.tmp/`

## Step 2: Write the Execution Script

Create `execution/fetch_weather.py`:

```python
"""Fetch current weather data for a city."""
import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def validate_output_path(path_str: str) -> Path:
    """Ensure output path is under PROJECT_ROOT/.tmp/."""
    allowed_dir = str((PROJECT_ROOT / ".tmp").resolve())
    resolved = str(Path(path_str).resolve())
    if not resolved.startswith(allowed_dir + os.sep):
        emit_error("path_violation", f"Output path must be under {allowed_dir}")
    return Path(path_str)


def emit_error(error_code: str, message: str):
    """Emit structured error JSON and exit."""
    print(json.dumps({"status": "error", "error_code": error_code, "message": message}))
    sys.exit(1)


def fetch_weather(city: str) -> dict:
    """Fetch weather data from API."""
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        emit_error("missing_key", "WEATHER_API_KEY not set in .env")

    url = f"https://api.weatherapi.com/v1/current.json?key={api_key}&q={city}"
    resp = requests.get(url, timeout=10)

    if resp.status_code == 400:
        emit_error("city_not_found", f"City not found: {city}")
    if resp.status_code == 429:
        emit_error("rate_limited", "API rate limit exceeded")
    resp.raise_for_status()

    return resp.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True, help="City name to look up")
    parser.add_argument("--output", required=True, help="Output JSON path (must be under .tmp/)")
    args = parser.parse_args()

    output_path = validate_output_path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = fetch_weather(args.city)

    output_path.write_text(json.dumps(data, indent=2))
    print(json.dumps({"status": "success", "city": args.city, "output": str(output_path)}))


if __name__ == "__main__":
    main()
```

**Key patterns demonstrated:**
- **Structured JSON output** on all paths (success and error) — the agent can parse the result
- **Path sandboxing** — output is validated to be under `.tmp/`
- **`emit_error()` helper** — consistent error format with semantic error codes
- **Exit codes** — 0 on success, non-zero on error

See `CLAUDE.md` for the full execution script standards.

## Step 3: Test It

Open your AI tool in this project and say:

```
> Check the weather in London
```

The agent will:
1. Read `directives/check_weather.md`
2. Run `execution/fetch_weather.py --city "London" --output .tmp/weather_result.json`
3. Parse the structured JSON output
4. Report the results to you

## Step 4: Self-Anneal

When something breaks (and it will), the self-annealing loop kicks in:

1. **Error happens** — the script returns structured error JSON
2. **Agent reads the error** — the error code tells it what went wrong
3. **Agent fixes the script** — adjusts the code to handle the issue
4. **Agent tests again** — re-runs to verify the fix
5. **Agent updates the directive** — adds the new edge case so it's documented for next time

For example, if the weather API starts requiring URL-encoded city names with spaces, the agent would:
- See the error from the API
- Add `urllib.parse.quote()` to the script
- Test with "New York" to verify
- Add a note to the directive: "City names with spaces are URL-encoded automatically"

This is the core development loop. The system gets stronger every time something breaks.

## Next Steps

- Add more directives and scripts as your project grows
- Use [Compound Engineering](https://github.com/EveryInc/compound-engineering-plugin) for complex multi-step features (see README.md)
- Check `CLAUDE.md` for execution script standards (security, performance, agent-native output patterns)
