# fma-tools

Delivery gates for Full Measure Advisory packs. Three tools and a doctor, one `fma`
command, invoked by an agent — the humans see results, never commands.

- **`fma read-ledger <export.xlsx>`** — read a Xero or Excel export safely. Xero writes
  every subtotal as a formula and caches zero, so a cold read returns figures that are
  all wrong in the same direction; this tool evaluates the formulas itself and refuses
  anything it cannot prove. Surfaces the header date (`--expect-date` turns a
  wrong-dated export into a refusal instead of a wrong pack).
- **`fma reconcile <mode> ...`** — run a pack's arithmetic ties and refuse if one
  breaks. Exit 1 lists every broken tie; a broken tie stops the pack. Never adjust a
  figure to make it tie.
- **`fma render <pack.html> --pdf/--docx/--pptx`** — turn finished HTML into the
  deliverable bytes, read the artifact back, and delete anything that fails its own
  gates (a deck without editable text runs never ships).
- **`fma doctor [--fix] [--deep]`** — say exactly what is missing and how to install
  it. Every FAIL carries one copy-pasteable fix line.

## Install (any Mac, run by the agent)

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --python 3.12 git+https://github.com/lawsonbanks/fma-tools
fma doctor --fix
fma doctor        # must exit 0 before first real use
```

Upgrade: `uv tool upgrade fma-tools && fma doctor`.

## Contract

Every invocation prints a JSON envelope to stdout
(`{tool, version, status, data, problems, warnings}`) and a one-line human summary to
stderr. Exit codes: 0 pass · 1 refuse (the gate did its job) · 2 cannot read input ·
3 environment missing · 4 internal bug. All paths are absolute arguments; the tools
discover nothing and write nowhere except the paths they are given.

This repo is public and never contains client data: no workbook is committed (test
fixtures are built in-test) and a test refuses any client name in source or tests.

## Develop

```sh
uv sync
uv run playwright install chromium
uv run pytest
```
