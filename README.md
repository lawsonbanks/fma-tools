# fma-tools

Delivery gates for Full Measure Advisory packs. Four tools and a doctor, one `fma`
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
- **`fma xero pull --as-at <date> --all --out <folder>`** — every connected Xero
  organisation's trial balance, balance sheet, profit and loss and bank summary at one
  typed date, read-only, in the shape `read-ledger` already reads. No date, no pull;
  a date Xero does not echo back, or a total that does not foot, and nothing is
  written. `fma xero group` lays the organisations of a pull side by side. See
  [Xero](#xero).
- **`fma doctor [--fix] [--deep] [--xero]`** — say exactly what is missing and how to
  install it. Every FAIL carries one copy-pasteable fix line.

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

One stated exception: `fma xero` keeps its app's client id and its rotating sign-in in
`~/.config/fma/xero/` (override with `FMA_CONFIG_DIR`), folder 700 and files 600. It is
the only tool that uses the network, and `fma xero auth` is the only step in all of fma
that needs a person at a browser. Where that store is put is the owner's decision: see
[A sign-in kept in a shared folder](#a-sign-in-kept-in-a-shared-folder).

This repo is public and never contains client data: no workbook is committed (test
fixtures are built in-test) and a test refuses any client name in source or tests.

## Xero

A read-only connection of our own to Xero's accounting API, for a group of companies
that each keep their own Xero organisation.

**Once per Mac.** Register an app at developer.xero.com of the type *Auth Code with
PKCE* (it has no client secret) with the redirect address
`http://localhost:8976/callback`, then:

```sh
fma xero config --client-id <the app's client id>
```

**Once per organisation.** Xero grants one organisation per consent, so this is run
once for each. A browser opens on Xero's own sign-in page; a person signs in, picks
ONE organisation and clicks Allow. No password, code or token passes through the agent.

```sh
fma xero auth --key ENTA --expect-org "Entity A Pty Ltd"
fma xero accounts            # what is connected; is each sign-in alive
```

`--paste` prints the link instead of listening, for a consent given on another device;
`fma xero auth --redirect '<the address the browser landed on>'` finishes it.

An organisation is always named in full, by its key or its Xero name: part of a name
never matches, and a name two organisations share is a refusal that lists both. If Xero
does not say which organisation a consent granted, nothing is guessed: the refusal
lists what that login holds, and `--expect-org "<its name>"` takes the one meant.

**Every time.**

```sh
fma xero pull --as-at 2026-06-30 --compare 2026-05-31 --all \
              --prefix ACME --out "<absolute folder, new or empty>"
fma xero group --pull "<that folder>" --as-at 2026-06-30 --out "<absolute .xlsx>"
```

What a pull guarantees, or it writes nothing:

- **The date was typed.** There is no default date. The financial-year ranges come from
  the typed date and each organisation's own year end.
- **Xero echoed it.** Each report's title line is parsed and must name the dates asked.
- **It foots and it agrees.** Each section's total is the sum of its lines; trial
  balance debits equal credits; net assets equal equity; year-to-date net profit equals
  Current Year Earnings on the balance sheet. All breaks are listed in one run.
- **It reads back.** Each workbook is read through `read-ledger`'s own loader before it
  is kept, so `fma read-ledger <file> --expect-date <date>` passes on every dated file.
  `read-ledger` recognises a pulled workbook only while it sits unchanged beside its
  `PULL.json`: edited, re-saved or moved, it is warned about like any hand-edited file.
- **It is whole.** A folder holds a complete pull with its record (`PULL.json`,
  `PULL.md`, and Xero's responses untouched under `raw/`) or it holds nothing.

What a pull does not contain, and says so in its own record: general ledger detail
("Account Transactions" — Xero serves it only on its highest developer tier) and Xero's
ageing columns (they exist only in the on-screen report). Those stay hand exports.

The group sheet is a management aggregation: each organisation's year-to-date trial
balance by account code, their plain sum, and an empty Eliminations column. It is not
a consolidation and not statutory accounts, and says so on its face. Two accounts share
a line only when they carry the same code; an account with no code stands on a line of
its own, and a name is never searched for something that looks like a code. Differences
between the charts of accounts are listed on a second sheet, never guessed; an optional
`--mapping` CSV (`entity, code, group_code[, group_name][, intercompany]`) says which
codes belong together, and a line naming a code the chart does not have refuses. The
pull must hold both the trial balance and the chart. An existing sheet is not written
over without `--replace`: it may hold eliminations someone typed in.

Limits worth knowing: the scopes requested are granular and read-only (nothing here
can change a ledger); Xero's free developer tier holds five organisations per app and
1,000 calls per organisation per day; an unused sign-in lapses after 60 days and any
pull or `fma xero accounts` keeps it alive; once lapsed, `fma xero auth` and one sign-in
renews every organisation that login holds; `fma xero disconnect --org <key>` withdraws
one organisation from this side, and lets go of a connection Xero still holds for the
app but this Mac does not when it is named in full.

### A sign-in kept in a shared folder

An agent that has a shared folder and nothing else (no installed tool, no home folder
that lasts) can still pull, if the people who own the books decide the sign-in may live
in that folder. That is their decision and not this tool's: anyone who can open the
folder can then read the connected organisations until the app is disconnected in Xero.

```sh
export FMA_CONFIG_DIR="<absolute path of a folder inside the shared folder>"
fma xero config --client-id <the app's client id> --shared
fma xero auth --paste          # prints a link; a person clicks Allow in any browser
fma xero auth --redirect '<the address that browser landed on>'
```

`--shared` records that the store is shared on purpose, so `fma doctor` does not fail
it for file modes a synced drive cannot keep. A folder that refuses modes, locks or
fsync is survived; one session at a time is then the rule. Keep one sign-in per place
it is used: a Mac's own store and a shared store each get their own Allow, so neither
spends the other's rotating token.

The agent's install needs no heavy dependency and any Python from 3.10:

```sh
python3 -m pip install --no-deps https://github.com/lawsonbanks/fma-tools/archive/refs/heads/main.zip
python3 -m pip install openpyxl certifi jsonschema
python3 -m fma_tools.cli xero accounts
```

That install runs `xero` and `read-ledger`; `render` needs the full install above. A
drive that requires its own fields at the head of every file can have them on the
pull's record: `fma xero pull ... --front-matter world=0`.

## Develop

```sh
uv sync
uv run playwright install chromium
uv run pytest
```

No test reaches the network, a real sign-in or a real browser: conftest points
`FMA_CONFIG_DIR` at a temp folder, replaces the transport with a stub that raises
unless a test injects the in-memory Xero from `tests/xero_fakes.py`, and fails any test
that reaches for a browser it did not stub.
