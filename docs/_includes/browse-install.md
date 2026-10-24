kolsch requires Python 3.12 or 3.13 (3.14 isn't supported yet); if uv
picks 3.14, add `--python 3.13` to the `uv tool install` command.

The command depends on how kolsch is installed in your environment.
Pick the section that matches yours:

### From a source checkout

For contributors running kolsch out of a local clone (`uv run kolsch`),
the `browse` extra installs into the project venv:

```bash
cd <path to your kolsch checkout>
uv sync --extra browse
uv run playwright install chromium
```

The extra is a PEP 621 `[project.optional-dependencies]` entry, so
the uv verb is `--extra`, not `--group`. The second command downloads
the ~150 MB Chromium binary that Playwright drives.

### Tool-installed (`uv tool install`)

```bash
uv tool install 'kolsch[browse]'
playwright install chromium
```

### pip

```bash
pip install 'kolsch[browse]'
playwright install chromium
```

!!! note "The LLM sees the same command"

    Since qol-090, `browse_url` failures (both
    `BrowseUrlExtraNotInstalled` and `BrowserBinaryMissing`) carry a
    `recovery_hint` that flows through the `tool.failed` event to the
    LLM. The agent tells you the exact command for your detected
    install mode — no guessing. `kolsch doctor` (qol-089) uses the
    same helper for a proactive read-only report.

### Container images

If you ship kolsch inside a container, bake the Chromium binary into
the image so users don't hit a mid-chat 150 MB download:

```dockerfile
RUN uv tool install 'kolsch[browse]' && playwright install chromium
```
