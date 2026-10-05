"""Which organisations are connected, and which one a caller means.

Xero calls an organisation a tenant. One sign-in can cover several, and Xero lists
them in no order that means anything. An earlier generation of this kind of tool kept
"the first one" and pointed a client's report at a demo company. So here an
organisation is always named: by the short key a person gave it (`NSW`), by its Xero
name, or by its id. A name that matches two organisations is a refusal that lists
both; a call that names none is a refusal too.
"""

from __future__ import annotations

import re

from ..errors import Refusal
from . import store

FREE_TIER_CAP = 5      # organisations one app may hold on Xero's free tier


def registry() -> dict:
    """tenant id -> row."""
    return dict(store.load(store.TENANTS).get("tenants") or {})


def save_registry(rows: dict) -> None:
    store.save(store.TENANTS, {"tenants": rows})


def slug(name: str) -> str:
    """A file-name-safe key from a Xero organisation name, for when nobody chose one."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")
    return s[:40] or "ORG"


def key_of(row: dict) -> str:
    return row.get("key") or slug(row.get("name", ""))


def _candidates(rows: dict, wanted: str) -> list[str]:
    w = wanted.strip().casefold()
    exact = [tid for tid, r in rows.items()
             if w in (tid.casefold(), key_of(r).casefold(), (r.get("name") or "").casefold())]
    if exact:
        return exact
    return [tid for tid, r in rows.items() if w and w in (r.get("name") or "").casefold()]


def _listing(rows: dict, ids=None) -> str:
    ids = list(rows) if ids is None else ids
    return ", ".join(f"{key_of(rows[t])} ({rows[t].get('name')})" for t in ids) or "none"


def find(wanted: str, rows: dict | None = None) -> str:
    """The one tenant id `wanted` names, or a refusal."""
    rows = registry() if rows is None else rows
    hits = _candidates(rows, wanted)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise Refusal("ORG_UNKNOWN",
                      f"no connected organisation is called {wanted!r}. "
                      f"Connected: {_listing(rows)}")
    raise Refusal("ORG_AMBIGUOUS",
                  f"{wanted!r} matches more than one organisation "
                  f"({_listing(rows, hits)}). Name it by its key.")


def resolve(orgs: list[str] | None, everything: bool) -> list[tuple[str, dict]]:
    """[(tenant id, row)] for `--org ...` or `--all`, in a stable order. Naming
    nothing is a refusal even when only one organisation is connected: the caller
    says which books it means."""
    rows = registry()
    if not rows:
        raise Refusal("ORG_NONE_CONNECTED",
                      f"no organisation is connected on this Mac yet. Run: {store.AUTH_FIX}")
    if everything and orgs:
        raise Refusal("ORG_REQUIRED", "give --all or --org, not both")
    if everything:
        ids = sorted(rows, key=lambda t: key_of(rows[t]).casefold())
    elif orgs:
        ids = []
        for o in orgs:
            t = find(o, rows)
            if t not in ids:
                ids.append(t)
    else:
        raise Refusal("ORG_REQUIRED",
                      "say which organisation: --org <key> (repeatable) or --all. "
                      f"Connected: {_listing(rows)}")
    keys = [key_of(rows[t]).casefold() for t in ids]
    if len(set(keys)) != len(keys):
        raise Refusal("ORG_KEY_CLASH",
                      "two of these organisations share a key, so their files would "
                      "overwrite each other. Give each its own: "
                      "fma xero accounts --set-key <name> <KEY>")
    return [(t, rows[t]) for t in ids]
