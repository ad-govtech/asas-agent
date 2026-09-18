# Recruiting console

A small app that runs on `asas-agent`, to show and test the platform: three preconfigured agents, a few real cases, and controls that change what runs.

```bash
# from the repo root, with a Postgres to hand
uv run asas-agent migrate
uv run python -m examples.recruiting.seed     # publishes the three agents into dev
uv run python -m examples.recruiting.app      # http://localhost:8010
```

Set `ASAS_PROMPT_PROVIDER=file` and `ASAS_PROMPT_DIR=examples/recruiting/prompts` to use the prompts in this folder, or upload them to Langfuse under the same names and leave the provider on `langfuse`.

## What it shows

| Control | What it proves |
|---|---|
| Agent dropdown | Three agents, one runtime. Screening, job descriptions and interview plans differ only by configuration |
| Case dropdown | The business service loads the application, requisition and candidate. The agent is asked for judgment, not for the data |
| Tools checkboxes | A caller can allow fewer tools than the definition lists, never more |
| Action tools switch | `interview.schedule` is refused unless the caller enables it, because it changes data |
| Model and temperature | Save as a new version, promote it, and the next run uses it. No deployment |
| Version list | Every version kept, with the live one marked. One click rolls back |
| What ran | Agent version, prompt name and version, model, tools, turn limit, latency, trace |

## Without a model key

The console still reads the real definition from the registry and resolves the real prompt, then shows a sample answer built from the same case data, marked as a sample. Set `OPENAI_API_KEY` (or the gateway settings) and the same controls run the agent for real.
