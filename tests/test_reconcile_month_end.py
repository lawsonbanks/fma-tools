"""month_end: the identity set, each family proven RED; CYE; disclosures; display."""

import json

import pytest

from reconcile_fixtures import make_contract


@pytest.fixture
def run_month_end(run_cli, tmp_path):
    def _run(contract: dict, display: dict | None = None):
        p = tmp_path / "contract.json"
        p.write_text(json.dumps(contract))
        argv = ["reconcile", "month_end", "--contract", str(p)]
        if display is not None:
            dp = tmp_path / "display.json"
            dp.write_text(json.dumps(display))
            argv += ["--display", str(dp)]
        return run_cli(argv)
    return _run


def _break_ids(env):
    return {b["id"] for b in env["data"]["breaks"]}


def test_passing_contract_ties(run_month_end):
    code, env = run_month_end(make_contract())
    assert code == 0, env["problems"]
    assert env["data"]["breaks"] == []
    assert env["data"]["n_checks"] > 55


# one mutation per identity family, each RED with the matching check id
@pytest.mark.parametrize("mutate,expect_id", [
    (lambda c: c["pnl"]["month_actual"].__setitem__("gross_profit", 1.0),
     "pnl.month_actual.gross_profit"),
    (lambda c: c["pnl"]["prior_fy"].__setitem__("npat", 1.0), "pnl.prior_fy.npat"),
    (lambda c: c["balance_sheet"]["current"]["assets"].__setitem__("total_assets", 1.0),
     "bs.current.total_assets"),
    (lambda c: c["balance_sheet"]["prior_month"]["equity"].__setitem__("share_capital", 1.0),
     "bs.prior_month.equity"),
    (lambda c: c["cash_flow"]["operating"].__setitem__("net_operating", 1.0),
     "cf.operating"),
    (lambda c: c["cash_flow"].__setitem__("net_movement", 1.0), "cf.net_movement"),
    (lambda c: c["aged_receivables"]["buckets"].__setitem__("d1_30", 1.0),
     "aged.buckets_total"),
    (lambda c: c["flows"].__setitem__("capex", 9999.0), "ppe_roll"),
    (lambda c: c["flows"].__setitem__("amortisation", 9999.0), "intangibles_roll"),
    (lambda c: c["cash_flow"]["operating"].__setitem__("working_capital_movement", 1.0),
     "cf.working_capital"),
    (lambda c: c["balance_sheet"]["current"]["equity"].__setitem__("retained_earnings", 1.0),
     "re_roll"),
])
def test_each_identity_family_red(run_month_end, mutate, expect_id):
    c = make_contract()
    mutate(c)
    code, env = run_month_end(c)
    assert code == 1
    assert env["status"] == "refuse"
    assert expect_id in _break_ids(env), _break_ids(env)
    assert all(p["code"] == "TIE_BROKEN" for p in env["problems"])


def test_a_equals_l_plus_e_red(run_month_end):
    c = make_contract()
    # move total_assets AND its footing so only A=L+E (and CF cash ties) can catch it
    a = c["balance_sheet"]["current"]["assets"]
    a["cash"] += 500.0
    a["total_current_assets"] += 500.0
    a["total_assets"] += 500.0
    code, env = run_month_end(c)
    assert code == 1
    assert "bs.current.balances" in _break_ids(env)


def test_cye_gap_carries_the_twice_diagnostic(run_month_end):
    c = make_contract()
    c["xero_controls"]["current_year_earnings"] = c["pnl"]["ytd_actual"]["npat"] - 671087.46
    code, env = run_month_end(c)
    assert code == 1
    brk = next(b for b in env["data"]["breaks"] if b["id"] == "cye_tie")
    assert "TWICE" in brk["detail"]
    assert "never adjust" in brk["detail"].lower()


def test_cye_cannot_be_disclosed_away(run_month_end):
    c = make_contract()
    c["xero_controls"]["current_year_earnings"] -= 100.0
    c["disclosed_differences"] = [{"check": "cye_tie", "amount": 100.0,
                                   "note": "trying to plug the control total"}]
    code, env = run_month_end(c)
    assert code == 2
    assert "cannot be disclosed away" in env["problems"][0]["message"]


def test_missing_key_is_schema_refusal_with_pointer(run_month_end):
    c = make_contract()
    del c["pnl"]["ytd_actual"]["npat"]
    code, env = run_month_end(c)
    assert code == 2
    assert env["problems"][0]["code"] == "CONTRACT_INVALID"
    assert "/pnl/ytd_actual" in env["problems"][0]["message"]


def test_missing_flows_is_schema_refusal(run_month_end):
    c = make_contract()
    del c["flows"]
    code, env = run_month_end(c)
    assert code == 2


def test_missing_xero_controls_is_schema_refusal(run_month_end):
    c = make_contract()
    del c["xero_controls"]
    code, env = run_month_end(c)
    assert code == 2


def test_unknown_tax_basis_refuses(run_month_end):
    c = make_contract()
    c["tax_basis"] = "modeled"          # the documented typo
    code, env = run_month_end(c)
    assert code == 2
    assert "modeled" in env["problems"][0]["message"]


# ── disclosed differences ────────────────────────────────────────────────────

def test_disclosure_closes_the_ar_gap_and_is_echoed(run_month_end):
    code, env = run_month_end(make_contract(ar_gap=101301.0))
    assert code == 0, env["problems"]
    applied = env["data"]["disclosures_applied"]
    assert applied and applied[0]["check"] == "aged_ar_control"
    assert "suspense" in applied[0]["notes"][0]


def test_disclosure_without_note_refuses(run_month_end):
    c = make_contract(ar_gap=100.0)
    c["disclosed_differences"][0]["note"] = "  "
    code, env = run_month_end(c)
    assert code == 2
    assert "plug" in env["problems"][0]["message"]


def test_disclosure_that_does_not_close_the_gap_still_fails(run_month_end):
    c = make_contract(ar_gap=100.0)
    c["disclosed_differences"][0]["amount"] = 40.0
    code, env = run_month_end(c)
    assert code == 1
    assert "aged_ar_control" in _break_ids(env)


def test_disclosure_against_unknown_check_refuses(run_month_end):
    c = make_contract()
    c["disclosed_differences"] = [{"check": "no_such_tie", "amount": 1.0,
                                   "note": "typo protection"}]
    code, env = run_month_end(c)
    assert code == 2
    assert "no_such_tie" in env["problems"][0]["message"]


# ── the display (round-once) verification ────────────────────────────────────

def _display_copy(c: dict) -> dict:
    """A correct whole-dollar display copy built the way the METHOD demands: round
    the input leaves once, derive every dependent figure, absorb residuals into the
    named plugs."""
    import copy
    d = copy.deepcopy(c)
    R = lambda x: float(round(x))

    for label, p in d["pnl"].items():
        for k in ("revenue", "cogs", "depreciation_amortisation", "net_interest",
                  "income_tax"):
            p[k] = R(p[k])
        for k in list(p["opex"]):
            p["opex"][k] = R(p["opex"][k])
        p["gross_profit"] = p["revenue"] - p["cogs"]
        p["opex_total"] = sum(p["opex"].values())
        p["ebitda"] = p["gross_profit"] - p["opex_total"]
        p["ebit"] = p["ebitda"] - p["depreciation_amortisation"]
        p["profit_before_tax"] = p["ebit"] - p["net_interest"]
        p["npat"] = p["profit_before_tax"] - p["income_tax"]

    fl = d["flows"]
    for k in list(fl):
        fl[k] = R(fl[k])
    # the CYE control prints as round(true) -- it is a quoted control, not a figure
    # derived from the rounded leaves
    d["xero_controls"]["current_year_earnings"] = R(c["xero_controls"]["current_year_earnings"])

    for side in ("prior_month", "current"):
        s = d["balance_sheet"][side]
        for grp in ("assets", "liabilities", "equity"):
            for k in list(s[grp]):
                s[grp][k] = R(s[grp][k])
        a, l, e = s["assets"], s["liabilities"], s["equity"]
        if side == "current":
            pa = d["balance_sheet"]["prior_month"]["assets"]
            a["ppe_net"] = pa["ppe_net"] + fl["capex"] - fl["depreciation_ppe"]
            a["intangibles"] = pa["intangibles"] - fl["amortisation"]
        a["total_current_assets"] = (a["cash"] + a["trade_receivables"]
                                     + a["work_in_progress"] + a["other_current_assets"])
        a["total_non_current_assets"] = (a["ppe_net"] + a["intangibles"]
                                         + a["other_non_current_assets"])
        a["total_assets"] = a["total_current_assets"] + a["total_non_current_assets"]
        l["total_current_liabilities"] = (l["trade_payables"] + l["gst_payg_payable"]
                                          + l["accrued_expenses"] + l["income_tax_payable"]
                                          + l["short_term_borrowings"])
        l["total_non_current_liabilities"] = l["long_term_borrowings"] + l["provisions"]
        l["total_liabilities"] = (l["total_current_liabilities"]
                                  + l["total_non_current_liabilities"])
        e["total_equity"] = a["total_assets"] - l["total_liabilities"]
        e["retained_earnings"] = e["total_equity"] - e["share_capital"]

    # tie the month result to the RE movement by plugging other_admin
    bs = d["balance_sheet"]
    re_move = (bs["current"]["equity"]["retained_earnings"]
               - bs["prior_month"]["equity"]["retained_earnings"])
    ja = d["pnl"]["month_actual"]
    ja["opex"]["other_admin"] += ja["npat"] - (re_move + fl.get("dividends", 0.0))
    ja["opex_total"] = sum(ja["opex"].values())
    ja["ebitda"] = ja["gross_profit"] - ja["opex_total"]
    ja["ebit"] = ja["ebitda"] - ja["depreciation_amortisation"]
    ja["profit_before_tax"] = ja["ebit"] - ja["net_interest"]
    ja["npat"] = ja["profit_before_tax"] - ja["income_tax"]

    # rebuild the cash flow from the rounded balance sheet + P&L
    cur, prior = bs["current"], bs["prior_month"]
    ca, cl, pa, pl = cur["assets"], cur["liabilities"], prior["assets"], prior["liabilities"]
    cf = d["cash_flow"]
    op, inv, fin = cf["operating"], cf["investing"], cf["financing"]
    cf["opening_cash"], cf["closing_cash"] = pa["cash"], ca["cash"]
    op["npat"] = ja["npat"]
    op["depreciation_amortisation"] = ja["depreciation_amortisation"]
    op["working_capital_movement"] = (
        -(ca["trade_receivables"] - pa["trade_receivables"])
        - (ca["work_in_progress"] - pa["work_in_progress"])
        - (ca["other_current_assets"] - pa["other_current_assets"])
        + (cl["trade_payables"] - pl["trade_payables"])
        + (cl["gst_payg_payable"] - pl["gst_payg_payable"])
        + (cl["accrued_expenses"] - pl["accrued_expenses"])
        + (cl["income_tax_payable"] - pl["income_tax_payable"])
        + (cl["provisions"] - pl["provisions"]))
    op["net_operating"] = (op["npat"] + op["depreciation_amortisation"]
                           + op["working_capital_movement"])
    inv["capex"] = -fl["capex"]
    inv["other_non_current_assets"] = -(ca["other_non_current_assets"]
                                        - pa["other_non_current_assets"])
    fl["other_non_current_assets_movement"] = (ca["other_non_current_assets"]
                                               - pa["other_non_current_assets"])
    inv["net_investing"] = inv["capex"] + inv["other_non_current_assets"]
    fin["loan_movement"] = ((cl["short_term_borrowings"] - pl["short_term_borrowings"])
                            + (cl["long_term_borrowings"] - pl["long_term_borrowings"]))
    fin["dividends"] = -fl.get("dividends", 0.0)
    fin["net_financing"] = fin["loan_movement"] + fin["dividends"]
    cf["net_movement"] = cf["closing_cash"] - cf["opening_cash"]

    # aged: round the young buckets, tie the total to the AR control, oldest plugs
    ar = d["aged_receivables"]
    b = ar["buckets"]
    disc = sum(x["amount"] for x in d.get("disclosed_differences", [])
               if x["check"] == "aged_ar_control")
    for x in d.get("disclosed_differences", []):
        x["amount"] = R(x["amount"])
    b["total"] = ca["trade_receivables"] + R(disc)
    for k in ("current", "d1_30", "d31_60", "d61_90"):
        b[k] = R(b[k])
    b["d90_plus"] = b["total"] - b["current"] - b["d1_30"] - b["d31_60"] - b["d61_90"]
    ar["overdue_total"] = b["d1_30"] + b["d31_60"] + b["d61_90"] + b["d90_plus"]
    for t in ar.get("top_overdue", []):
        t["amount"] = R(t["amount"])
    return d


def _uneven_contract() -> dict:
    """A contract with cents everywhere, so rounding has real work to do."""
    c = make_contract()
    c["pnl"]["month_actual"]["revenue"] += 0.37
    c["pnl"]["month_actual"]["gross_profit"] += 0.37
    c["pnl"]["month_actual"]["ebitda"] += 0.37
    c["pnl"]["month_actual"]["ebit"] += 0.37
    c["pnl"]["month_actual"]["profit_before_tax"] += 0.37
    c["pnl"]["month_actual"]["npat"] += 0.37
    # keep the articulation: RE roll, CF npat, net_operating, cash
    c["balance_sheet"]["current"]["equity"]["retained_earnings"] += 0.37
    c["balance_sheet"]["current"]["equity"]["total_equity"] += 0.37
    c["cash_flow"]["operating"]["npat"] += 0.37
    c["cash_flow"]["operating"]["net_operating"] += 0.37
    c["cash_flow"]["closing_cash"] += 0.37
    c["cash_flow"]["net_movement"] += 0.37
    c["balance_sheet"]["current"]["assets"]["cash"] += 0.37
    c["balance_sheet"]["current"]["assets"]["total_current_assets"] += 0.37
    c["balance_sheet"]["current"]["assets"]["total_assets"] += 0.37
    return c


def test_display_correct_rounding_passes(run_month_end):
    c = _uneven_contract()
    code, env = run_month_end(c, display=_display_copy(c))
    assert code == 0, [p["message"] for p in env["problems"]][:6]


def test_display_non_plug_leaf_drift_is_red(run_month_end):
    c = _uneven_contract()
    d = _display_copy(c)
    # a leaf printed at a value that is NOT round(true) — the round-once break
    d["pnl"]["month_actual"]["revenue"] += 1.0
    d["pnl"]["month_actual"]["gross_profit"] += 1.0
    d["pnl"]["month_actual"]["ebitda"] += 1.0
    d["pnl"]["month_actual"]["ebit"] += 1.0
    d["pnl"]["month_actual"]["profit_before_tax"] += 1.0
    d["pnl"]["month_actual"]["npat"] += 1.0
    code, env = run_month_end(c, display=d)
    assert code == 1
    assert any(b["id"] == "display.leaf.pnl.month_actual.revenue"
               for b in env["data"]["breaks"])


def test_display_broken_tie_is_red(run_month_end):
    c = _uneven_contract()
    d = _display_copy(c)
    d["cash_flow"]["net_movement"] += 7.0
    code, env = run_month_end(c, display=d)
    assert code == 1
    assert any(b["id"].startswith("display.cf") for b in env["data"]["breaks"])


def test_display_plug_deltas_reported(run_month_end):
    c = _uneven_contract()
    code, env = run_month_end(c, display=_display_copy(c))
    assert code == 0
    plugs = [ch for ch in env["data"]["checks"] if ch["id"].startswith("display.plug.")]
    assert plugs and all(p["passed"] for p in plugs)
    assert any("delta" in (p.get("detail") or "") for p in plugs)


# ── the review-found display holes, each proven closed ───────────────────────

def test_display_cannot_drop_a_disclosed_finding(run_month_end):
    """A client-records gap the pack must print cannot be absorbed into d90_plus
    on the printed face by deleting the disclosure from the display copy."""
    c = make_contract(ar_gap=-1000.0)
    d = _display_copy(c)
    d["disclosed_differences"] = []
    b = d["aged_receivables"]["buckets"]
    control = d["balance_sheet"]["current"]["assets"]["trade_receivables"]
    b["total"] = control
    b["d90_plus"] = (b["total"] - b["current"] - b["d1_30"] - b["d31_60"]
                     - b["d61_90"])
    d["aged_receivables"]["overdue_total"] = (b["d1_30"] + b["d31_60"] + b["d61_90"]
                                              + b["d90_plus"])
    code, env = run_month_end(c, display=d)
    assert code == 1
    assert any(b_["id"] == "display.disclosure.aged_ar_control"
               for b_ in env["data"]["breaks"])


def test_display_cannot_fabricate_a_disclosure(run_month_end):
    c = make_contract()
    d = _display_copy(c)
    d["disclosed_differences"] = [{"check": "aged_ar_control", "amount": 500.0,
                                   "note": "invented for the face"}]
    b = d["aged_receivables"]["buckets"]
    b["total"] += 500.0
    b["d90_plus"] += 500.0
    d["aged_receivables"]["overdue_total"] += 500.0
    code, env = run_month_end(c, display=d)
    assert code == 1
    assert any(b_["id"] == "display.disclosure.aged_ar_control"
               for b_ in env["data"]["breaks"])


def test_display_cannot_overstate_ppe_and_equity(run_month_end):
    """+$50k on ppe_net and retained earnings on BOTH columns used to pass every
    display check; the prior-side leaves now pin it."""
    c = _uneven_contract()
    d = _display_copy(c)
    for side in ("current", "prior_month"):
        s = d["balance_sheet"][side]
        s["assets"]["ppe_net"] += 50000.0
        s["assets"]["total_non_current_assets"] += 50000.0
        s["assets"]["total_assets"] += 50000.0
        s["equity"]["retained_earnings"] += 50000.0
        s["equity"]["total_equity"] += 50000.0
    code, env = run_month_end(c, display=d)
    assert code == 1
    assert any("ppe_net" in b_["id"] for b_ in env["data"]["breaks"])


def test_display_cannot_shift_ytd_npat_through_opex(run_month_end):
    """-$50k off a YTD opex line with the chain re-derived and the display CYE moved
    to match used to pass; opex leaves + the CYE leaf now pin it."""
    c = _uneven_contract()
    d = _display_copy(c)
    p = d["pnl"]["ytd_actual"]
    p["opex"]["salaries_oncosts"] -= 50000.0
    p["opex_total"] -= 50000.0
    p["ebitda"] += 50000.0
    p["ebit"] += 50000.0
    p["profit_before_tax"] += 50000.0
    p["npat"] += 50000.0
    d["xero_controls"]["current_year_earnings"] = p["npat"]
    code, env = run_month_end(c, display=d)
    assert code == 1
    broken = {b_["id"] for b_ in env["data"]["breaks"]}
    assert ("display.leaf.pnl.ytd_actual.opex.salaries_oncosts" in broken
            or "display.leaf.xero_controls.current_year_earnings" in broken)


# ── review-found input holes ─────────────────────────────────────────────────

def test_nan_in_a_contract_refuses(run_cli, tmp_path):
    c = make_contract()
    text = __import__("json").dumps(c).replace(
        str(c["pnl"]["month_actual"]["revenue"]), "NaN", 1)
    p = tmp_path / "nan_contract.json"
    p.write_text(text)
    code, env = run_cli(["reconcile", "month_end", "--contract", str(p)])
    assert code == 2
    assert "NaN" in env["problems"][0]["message"]


def test_modelled_without_rate_refuses(run_month_end):
    c = make_contract()
    c["tax_basis"] = "modelled"
    c["flows"].pop("assumed_tax_rate", None)
    code, env = run_month_end(c)
    assert code == 2
    assert "assumed_tax_rate" in env["problems"][0]["message"]


def test_phantom_bucket_key_is_schema_refusal(run_month_end):
    c = make_contract()
    c["aged_receivables"]["buckets"]["parked"] = 100.0
    c["aged_receivables"]["buckets"]["total"] += 100.0
    code, env = run_month_end(c)
    assert code == 2
    assert env["problems"][0]["code"] == "CONTRACT_INVALID"
