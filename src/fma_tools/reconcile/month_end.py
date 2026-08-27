"""Month-end contract ties — the full identity set, ported and kept hand-written.

Every tie a chartered accountant checks reading the statements against each other,
enforced in code, so a mapping error is a refusal, not a shipped pack. The identities
stay explicit `if` statements deliberately: a generic expression language would hand
check-authoring back to the model, which is the failure this tool exists to prevent.

Changes from the ported engine, each deliberate:
  - period keys are generic (month_actual ... prior_fy), not a hardcoded month;
  - `flows` is REQUIRED (zeros allowed), so the roll-forward checks can never be
    silently skipped by an absent block;
  - the Current Year Earnings tie moved here from the old feed: the ledger's own
    year-to-date result is the one control total Xero computes independently of
    everything the pack does, so it is a required input, not an optional guard.
"""

from __future__ import annotations

from ..errors import InputProblem
from .lib import CENT, CheckResult, Disclosures, fact, tie

PERIODS = ("month_actual", "month_budget", "ytd_actual", "ytd_budget", "prior_fy")

# If the CYE tie breaks, stop and find the misclassified account. Never adjust the
# pack — and never disclose this one away.
NON_DISCLOSABLE = {"cye_tie"}

# The three named plugs display rounding is allowed to land in (the controller's own
# moves): equity's plug line, the largest expense line, the oldest ageing bucket.
DISPLAY_PLUGS = {
    ("balance_sheet", "current", "equity", "retained_earnings"),
    ("balance_sheet", "prior_month", "equity", "retained_earnings"),
    ("pnl", "month_actual", "opex", "other_admin"),
    ("aged_receivables", "buckets", "d90_plus"),
}


def _bucket_for_days(days):
    if days <= 30:
        return "d1_30"
    if days <= 60:
        return "d31_60"
    if days <= 90:
        return "d61_90"
    return "d90_plus"


def _ne(a, b):
    return abs(a - b) > CENT


def _gt(a, b):
    return a - b > CENT


# ---------------------------------------------------------------- P&L (within)
def _pnl_checks(c) -> list[CheckResult]:
    """The P&L subtotal chain recomputes at every column, and (when tax is MODELLED)
    the effective rate matches the assumed statutory rate. A LIVE pack fed real Xero
    actuals carries booked tax that is never exactly PBT x rate, so tax_basis="actual"
    relaxes ONLY the rate check; "pre_tax" (no tax provided in-period) likewise. An
    unrecognised basis refuses rather than silently skipping the check it governs."""
    out = []
    tax_basis = c["tax_basis"]
    rate = c["flows"].get("assumed_tax_rate")
    for label in PERIODS:
        p = c["pnl"][label]
        out.append(tie(f"pnl.{label}.gross_profit", f"{label}: gross profit = revenue - COGS",
                       p["gross_profit"], p["revenue"] - p["cogs"]))
        out.append(tie(f"pnl.{label}.opex_total", f"{label}: opex_total = sum of opex lines",
                       p["opex_total"], sum(p["opex"].values())))
        out.append(tie(f"pnl.{label}.ebitda", f"{label}: EBITDA = gross profit - opex",
                       p["ebitda"], p["gross_profit"] - p["opex_total"]))
        out.append(tie(f"pnl.{label}.ebit", f"{label}: EBIT = EBITDA - D&A",
                       p["ebit"], p["ebitda"] - p["depreciation_amortisation"]))
        out.append(tie(f"pnl.{label}.pbt", f"{label}: PBT = EBIT - net interest",
                       p["profit_before_tax"], p["ebit"] - p["net_interest"]))
        out.append(tie(f"pnl.{label}.npat", f"{label}: NPAT = PBT - income tax",
                       p["npat"], p["profit_before_tax"] - p["income_tax"]))
        if tax_basis == "modelled" and rate and p["profit_before_tax"]:
            eff = p["income_tax"] / p["profit_before_tax"]
            out.append(fact(f"pnl.{label}.effective_tax",
                            f"{label}: effective tax rate within 1pp of assumed",
                            abs(eff - rate) <= 0.01,
                            f"effective {eff:.3f} vs assumed {rate}"))
    return out


# ---------------------------------------------------------------- Balance sheet (within)
def _balance_sheet_checks(c) -> list[CheckResult]:
    """Every subtotal foots from its lines, and A = L + E, on both columns."""
    out = []
    for label in ("current", "prior_month"):
        side = c["balance_sheet"][label]
        a, l, e = side["assets"], side["liabilities"], side["equity"]
        out.append(tie(f"bs.{label}.current_assets", f"{label}: current assets foot",
                       a["total_current_assets"],
                       a["cash"] + a["trade_receivables"] + a["work_in_progress"]
                       + a["other_current_assets"]))
        out.append(tie(f"bs.{label}.non_current_assets", f"{label}: non-current assets foot",
                       a["total_non_current_assets"],
                       a["ppe_net"] + a["intangibles"] + a.get("other_non_current_assets", 0)))
        out.append(tie(f"bs.{label}.total_assets", f"{label}: total assets foot",
                       a["total_assets"],
                       a["total_current_assets"] + a["total_non_current_assets"]))
        out.append(tie(f"bs.{label}.current_liabilities", f"{label}: current liabilities foot",
                       l["total_current_liabilities"],
                       l["trade_payables"] + l["gst_payg_payable"] + l["accrued_expenses"]
                       + l.get("income_tax_payable", 0) + l["short_term_borrowings"]))
        out.append(tie(f"bs.{label}.non_current_liabilities",
                       f"{label}: non-current liabilities foot",
                       l["total_non_current_liabilities"],
                       l["long_term_borrowings"] + l["provisions"]))
        out.append(tie(f"bs.{label}.total_liabilities", f"{label}: total liabilities foot",
                       l["total_liabilities"],
                       l["total_current_liabilities"] + l["total_non_current_liabilities"]))
        out.append(tie(f"bs.{label}.equity", f"{label}: equity foots",
                       e["total_equity"], e["share_capital"] + e["retained_earnings"]))
        out.append(tie(f"bs.{label}.balances", f"{label}: A = L + E",
                       a["total_assets"], l["total_liabilities"] + e["total_equity"]))
    return out


# ---------------------------------------------------------------- Cash flow (within)
def _cash_flow_checks(c) -> list[CheckResult]:
    """The cash-flow statement articulates on its own terms."""
    cf = c["cash_flow"]
    op, inv, fin = cf["operating"], cf["investing"], cf["financing"]
    return [
        tie("cf.operating", "CF operating articulates (NPAT + D&A + WC)",
            op["net_operating"],
            op["npat"] + op["depreciation_amortisation"] + op["working_capital_movement"]),
        tie("cf.investing", "CF investing foots to its lines",
            inv["net_investing"], inv["capex"] + inv.get("other_non_current_assets", 0)),
        tie("cf.financing", "CF financing foots to its lines (loans + dividends)",
            fin["net_financing"], fin["loan_movement"] + fin.get("dividends", 0)),
        tie("cf.net_movement", "CF activities sum to the net movement",
            op["net_operating"] + inv["net_investing"] + fin["net_financing"],
            cf["net_movement"]),
        tie("cf.opening_closing", "opening + movement = closing",
            cf["opening_cash"] + cf["net_movement"], cf["closing_cash"]),
    ]


# ---------------------------------------------------------------- Aged (within)
def _aged_checks(c) -> list[CheckResult]:
    """Buckets sum to the total, overdue recomputes from the past-due bands, and
    every named overdue account is positive, genuinely overdue, and fits inside its
    own bucket (individually and summed). Containment only bites on a POSITIVE
    bucket: one driven net-negative by unapplied credit notes cannot 'contain' a
    real named overdue — the credit position is a disclosed data item, not a
    fabricated exposure."""
    out = []
    ar = c["aged_receivables"]
    b = ar["buckets"]
    out.append(tie("aged.buckets_total", "aged buckets sum to total",
                   sum(v for k, v in b.items() if k != "total"), b["total"]))
    out.append(tie("aged.overdue_total", "overdue_total = sum of past-due buckets",
                   ar["overdue_total"],
                   b["d1_30"] + b["d31_60"] + b["d61_90"] + b["d90_plus"]))
    per_bucket: dict[str, float] = {}
    for t in ar.get("top_overdue", []):
        who = t["customer"]
        out.append(fact(f"aged.top.{who}.positive", f"top-overdue {who}: amount positive",
                        t["amount"] > 0, f"amount {t['amount']}"))
        out.append(fact(f"aged.top.{who}.overdue", f"top-overdue {who}: genuinely overdue",
                        t["days"] >= 1, f"days {t['days']}"))
        bk = _bucket_for_days(t["days"])
        per_bucket[bk] = per_bucket.get(bk, 0) + t["amount"]
        if b[bk] >= 0:
            out.append(fact(f"aged.top.{who}.contained",
                            f"top-overdue {who}: fits its bucket {bk}",
                            not _gt(t["amount"], b[bk]),
                            f"{t['amount']} vs bucket {b[bk]}"))
    for bk, tot in per_bucket.items():
        if b[bk] >= 0:
            out.append(fact(f"aged.named_within.{bk}",
                            f"named overdue within {bk} fits the bucket",
                            not _gt(tot, b[bk]), f"named {tot} vs bucket {b[bk]}"))
    return out


# ---------------------------------------------------------------- Cross-statement
def _consolidation_checks(c, disc: Disclosures) -> list[CheckResult]:
    """The ties that only exist once the pieces sit together, exactly as a controller
    reconciles the pack. `flows` is required, so nothing here can silently thin."""
    out = []
    bs = c["balance_sheet"]
    cur, prior = bs["current"], bs["prior_month"]
    fl = c["flows"]
    pnl_m = c["pnl"]["month_actual"]

    out.append(tie("re_roll", "retained earnings roll = NPAT - dividends",
                   cur["equity"]["retained_earnings"] - prior["equity"]["retained_earnings"],
                   pnl_m["npat"] - fl.get("dividends", 0)))
    out.append(tie("ppe_roll", "PP&E roll: close = open + capex - depreciation",
                   cur["assets"]["ppe_net"],
                   prior["assets"]["ppe_net"] + fl["capex"] - fl["depreciation_ppe"]))
    out.append(tie("intangibles_roll", "intangibles roll: close = open - amortisation",
                   cur["assets"]["intangibles"],
                   prior["assets"]["intangibles"] - fl["amortisation"]))
    out.append(tie("da_split", "depreciation + amortisation = P&L D&A",
                   fl["depreciation_ppe"] + fl["amortisation"],
                   pnl_m["depreciation_amortisation"]))

    cf = c["cash_flow"]
    op = cf["operating"]
    out.append(tie("cf.npat", "CF NPAT = P&L NPAT", op["npat"], pnl_m["npat"]))
    out.append(tie("cf.da", "CF D&A = P&L D&A",
                   op["depreciation_amortisation"], pnl_m["depreciation_amortisation"]))
    ca_c, ca_p = cur["assets"], prior["assets"]
    cl_c, cl_p = cur["liabilities"], prior["liabilities"]
    wc_from_bs = (
        -(ca_c["trade_receivables"] - ca_p["trade_receivables"])
        - (ca_c["work_in_progress"] - ca_p["work_in_progress"])
        - (ca_c["other_current_assets"] - ca_p["other_current_assets"])
        + (cl_c["trade_payables"] - cl_p["trade_payables"])
        + (cl_c["gst_payg_payable"] - cl_p["gst_payg_payable"])
        + (cl_c["accrued_expenses"] - cl_p["accrued_expenses"])
        + (cl_c.get("income_tax_payable", 0) - cl_p.get("income_tax_payable", 0))
        + (cl_c["provisions"] - cl_p["provisions"])
    )
    out.append(tie("cf.working_capital", "CF working capital = balance-sheet movement (no plug)",
                   op["working_capital_movement"], wc_from_bs))
    out.append(tie("cf.investing_flows", "CF investing = -(capex + non-current-asset movement)",
                   cf["investing"]["net_investing"],
                   -fl["capex"] - fl.get("other_non_current_assets_movement", 0)))
    onca_move_bs = (ca_c.get("other_non_current_assets", 0)
                    - ca_p.get("other_non_current_assets", 0))
    out.append(tie("cf.onca", "CF non-current-asset line = balance-sheet movement",
                   cf["investing"].get("other_non_current_assets", 0), -onca_move_bs))
    fin_from_bs = ((cl_c["short_term_borrowings"] - cl_p["short_term_borrowings"])
                   + (cl_c["long_term_borrowings"] - cl_p["long_term_borrowings"]))
    out.append(tie("cf.financing_flows",
                   "CF financing = borrowings movement + dividend flow",
                   cf["financing"]["net_financing"],
                   fin_from_bs + cf["financing"].get("dividends", 0)))
    out.append(tie("cf.closing_cash", "closing cash = balance sheet cash",
                   cf["closing_cash"], ca_c["cash"]))
    out.append(tie("cf.opening_cash", "opening cash = prior-month balance sheet cash",
                   cf["opening_cash"], ca_p["cash"]))

    # The aged subledger ties to the GL AR control AFTER any disclosed reconciling
    # difference — a named, shown item, never a silent plug.
    out.append(tie("aged_ar_control", "aged total ties to the AR control",
                   c["aged_receivables"]["buckets"]["total"],
                   cur["assets"]["trade_receivables"],
                   disclosed=disc.for_check("aged_ar_control")))
    return out


# ---------------------------------------------------------------- CYE (the gate)
def _cye_check(c) -> CheckResult:
    """The pack does not proceed unless the YTD result ties to Xero's own Current
    Year Earnings to the cent. CYE is the ledger's OWN year-to-date result — the one
    control total Xero computes independently of everything the pack does — so a
    mapping error surfaces here as a gap of exactly TWICE the misplaced line (an
    account booked on the wrong side of the P&L). Fix the account mapping; never
    adjust the figures. Compared against NPAT: CYE is the ledger's net result after
    whatever tax is booked, identical to PBT on a pre_tax basis."""
    cye = c["xero_controls"]["current_year_earnings"]
    ytd = c["pnl"]["ytd_actual"]["npat"]
    gap = ytd - cye
    detail = None
    if abs(gap) > CENT:
        detail = (f"gap {gap:,.2f} — a gap of exactly TWICE an account's value means "
                  "that account was booked on the wrong side of the P&L. Fix the "
                  "account map; never adjust the figures.")
    return tie("cye_tie", "YTD result ties to Xero's Current Year Earnings",
               ytd, cye, detail=detail)


def checks(c: dict, disc: Disclosures) -> list[CheckResult]:
    if c["tax_basis"] not in ("modelled", "actual", "pre_tax"):
        # Whitelist the basis: an unrecognised value (e.g. the typo "modeled") must
        # NOT silently skip the checks the field governs. Refuse instead.
        raise InputProblem("CONTRACT_INVALID",
                           f"unknown tax_basis {c['tax_basis']!r} "
                           "(expected 'modelled', 'actual' or 'pre_tax')")
    return ([_cye_check(c)] + _pnl_checks(c) + _balance_sheet_checks(c)
            + _cash_flow_checks(c) + _aged_checks(c) + _consolidation_checks(c, disc))


# ---------------------------------------------------------------- display verification
# The round-once discipline as VERIFICATION, not computation: the agent produces the
# whole-dollar display copy; this proves (a) every tie still holds on it, (b) every
# non-plug input leaf equals round(true), (c) residuals sit only in the named plugs.

_INPUT_LEAVES = (
    [("pnl", p, k) for p in PERIODS
     for k in ("revenue", "cogs", "depreciation_amortisation", "net_interest", "income_tax")]
    + [("balance_sheet", side, grp, k)
       for side in ("current", "prior_month")
       for grp, keys in (
           ("assets", ("cash", "trade_receivables", "work_in_progress",
                       "other_current_assets", "other_non_current_assets")),
           ("liabilities", ("trade_payables", "gst_payg_payable", "accrued_expenses",
                            "income_tax_payable", "short_term_borrowings",
                            "long_term_borrowings", "provisions")),
           ("equity", ("share_capital",)))
       for k in keys]
    + [("aged_receivables", "buckets", k) for k in ("current", "d1_30", "d31_60", "d61_90")]
    + [("flows", k) for k in ("capex", "depreciation_ppe", "amortisation", "dividends")]
)


def _dig(doc, path):
    v = doc
    for p in path:
        if not isinstance(v, dict) or p not in v:
            return None
        v = v[p]
    return v


def display_checks(true_c: dict, display_c: dict) -> list[CheckResult]:
    out = []
    # (a) the full tie set holds on the printed face (dollar values, so effectively
    # exact at CENT). Stronger than the engine this replaces, which skipped the
    # cross-statement gates on the rounded copy — safe then only because the
    # producer was trusted code; the producer is now untrusted. The display copy
    # states its own (rounded) disclosed differences.
    disp_disc = Disclosures(display_c.get("disclosed_differences"),
                            non_disclosable=NON_DISCLOSABLE)
    for chk in checks(display_c, disp_disc):
        chk.id = "display." + chk.id
        chk.name = "display: " + chk.name
        out.append(chk)
    disp_disc.assert_all_consumed()
    # (b) round-once fidelity on every non-plug input leaf
    for path in _INPUT_LEAVES:
        t, d = _dig(true_c, path), _dig(display_c, path)
        if t is None and d is None:
            continue
        dotted = ".".join(path)
        if t is None or d is None:
            out.append(fact(f"display.leaf.{dotted}", f"display leaf {dotted} present in both",
                            False, "present in one document only"))
            continue
        if path in DISPLAY_PLUGS:
            continue
        out.append(fact(f"display.leaf.{dotted}", f"display {dotted} = round(true)",
                        abs(d - round(t)) <= CENT, f"display {d} vs round({t}) = {round(t)}"))
    # (c) the plug deltas, reported (figures, not pass/fail)
    for path in sorted(DISPLAY_PLUGS):
        t, d = _dig(true_c, path), _dig(display_c, path)
        if t is None or d is None:
            continue
        dotted = ".".join(path)
        out.append(fact(f"display.plug.{dotted}", f"plug {dotted} absorbed the residual",
                        True, f"delta {round(d - round(t), 2):+}"))
    return out
