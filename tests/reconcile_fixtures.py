"""Synthetic, fully-articulating fixture documents for the reconcile modes.

Everything is derived from primitives the way the statements actually articulate, so
the factory produces a contract that passes every tie; each RED test then mutates
exactly one thing. All figures obviously fake.
"""

from __future__ import annotations

import json
from datetime import date


def make_pnl(revenue, cogs, opex, da, interest, tax):
    gp = revenue - cogs
    opex_total = sum(opex.values())
    ebitda = gp - opex_total
    ebit = ebitda - da
    pbt = ebit - interest
    return {"revenue": revenue, "cogs": cogs, "gross_profit": gp, "opex": dict(opex),
            "opex_total": opex_total, "ebitda": ebitda,
            "depreciation_amortisation": da, "ebit": ebit, "net_interest": interest,
            "profit_before_tax": pbt, "income_tax": tax, "npat": pbt - tax}


def make_contract(*, ar_gap: float = 0.0) -> dict:
    """A passing month-end contract. `ar_gap` puts a gap between the aged book and
    the AR control (to be disclosed, or to break the tie)."""
    capex, dep, amort, div = 500.0, 300.0, 100.0, 0.0
    da = dep + amort
    opex = {"salaries_oncosts": 4000.0, "occupancy": 800.0, "other_admin": 700.0}
    month = make_pnl(20000.0, 8000.0, opex, da, 50.0, 0.0)
    ytd = make_pnl(240000.0, 96000.0,
                   {k: v * 12 for k, v in opex.items()}, da * 12, 600.0, 0.0)

    prior_assets = {"cash": 5000.0, "trade_receivables": 7000.0,
                    "work_in_progress": 1000.0, "other_current_assets": 500.0,
                    "ppe_net": 9000.0, "intangibles": 2000.0,
                    "other_non_current_assets": 400.0}
    prior_liab = {"trade_payables": 3000.0, "gst_payg_payable": 900.0,
                  "accrued_expenses": 600.0, "income_tax_payable": 0.0,
                  "short_term_borrowings": 1200.0, "long_term_borrowings": 4000.0,
                  "provisions": 800.0}
    prior_re = 8000.0

    cur_assets = dict(prior_assets)
    cur_assets.update(trade_receivables=7600.0, work_in_progress=1100.0,
                      other_current_assets=450.0,
                      ppe_net=prior_assets["ppe_net"] + capex - dep,
                      intangibles=prior_assets["intangibles"] - amort,
                      other_non_current_assets=500.0)
    cur_liab = dict(prior_liab)
    cur_liab.update(trade_payables=3300.0, gst_payg_payable=950.0,
                    accrued_expenses=700.0, short_term_borrowings=1000.0,
                    long_term_borrowings=3800.0, provisions=850.0)

    d = lambda k, a, b: a[k] - b[k]
    wc = (-d("trade_receivables", cur_assets, prior_assets)
          - d("work_in_progress", cur_assets, prior_assets)
          - d("other_current_assets", cur_assets, prior_assets)
          + d("trade_payables", cur_liab, prior_liab)
          + d("gst_payg_payable", cur_liab, prior_liab)
          + d("accrued_expenses", cur_liab, prior_liab)
          + d("income_tax_payable", cur_liab, prior_liab)
          + d("provisions", cur_liab, prior_liab))
    onca_move = d("other_non_current_assets", cur_assets, prior_assets)
    net_op = month["npat"] + da + wc
    net_inv = -capex - onca_move
    loan_move = (d("short_term_borrowings", cur_liab, prior_liab)
                 + d("long_term_borrowings", cur_liab, prior_liab))
    net_fin = loan_move - div
    cur_assets["cash"] = prior_assets["cash"] + net_op + net_inv + net_fin
    cur_re = prior_re + month["npat"] - div

    def bs_side(assets, liab, re):
        tca = (assets["cash"] + assets["trade_receivables"]
               + assets["work_in_progress"] + assets["other_current_assets"])
        tnca = assets["ppe_net"] + assets["intangibles"] + assets["other_non_current_assets"]
        tcl = (liab["trade_payables"] + liab["gst_payg_payable"]
               + liab["accrued_expenses"] + liab["income_tax_payable"]
               + liab["short_term_borrowings"])
        tncl = liab["long_term_borrowings"] + liab["provisions"]
        ta, tl = tca + tnca, tcl + tncl
        sc = ta - tl - re
        return {"assets": {**assets, "total_current_assets": tca,
                           "total_non_current_assets": tnca, "total_assets": ta},
                "liabilities": {**liab, "total_current_liabilities": tcl,
                                "total_non_current_liabilities": tncl,
                                "total_liabilities": tl},
                "equity": {"share_capital": sc, "retained_earnings": re,
                           "total_equity": sc + re}}

    prior_side = bs_side(prior_assets, prior_liab, prior_re)
    cur_side = bs_side(cur_assets, cur_liab, cur_re)
    # share capital must not move between periods; force the prior RE to make both
    # sides carry the same share capital (the fixture's balancer)
    sc = cur_side["equity"]["share_capital"]
    prior_re = prior_side["equity"]["total_equity"] - sc
    # keep the retained-earnings roll intact: cur_re moves with the recomputed prior
    cur_re = prior_re + month["npat"] - div
    prior_side = bs_side(prior_assets, prior_liab, prior_re)
    cur_side = bs_side(cur_assets, cur_liab, cur_re)

    aged_total = cur_assets["trade_receivables"] + ar_gap
    buckets = {"current": round(aged_total - 2100.0, 2), "d1_30": 1000.0,
               "d31_60": 600.0, "d61_90": 300.0, "d90_plus": 200.0,
               "total": round(aged_total, 2)}
    return {
        "entity": "Fixture Pty Ltd", "book": "MAIN",
        "period": {"month": "2026-06", "label": "June 2026"},
        "tax_basis": "pre_tax",
        "pnl": {"month_actual": month,
                "month_budget": make_pnl(19000.0, 7600.0, opex, da, 50.0, 0.0),
                "ytd_actual": ytd,
                "ytd_budget": make_pnl(230000.0, 92000.0,
                                       {k: v * 12 for k, v in opex.items()},
                                       da * 12, 600.0, 0.0),
                "prior_fy": make_pnl(200000.0, 90000.0,
                                     {k: v * 11 for k, v in opex.items()},
                                     da * 11, 700.0, 9000.0)},
        "balance_sheet": {"current": cur_side, "prior_month": prior_side},
        "cash_flow": {
            "opening_cash": prior_assets["cash"], "closing_cash": cur_assets["cash"],
            "net_movement": cur_assets["cash"] - prior_assets["cash"],
            "operating": {"npat": month["npat"], "depreciation_amortisation": da,
                          "working_capital_movement": wc, "net_operating": net_op},
            "investing": {"capex": -capex, "other_non_current_assets": -onca_move,
                          "net_investing": net_inv},
            "financing": {"loan_movement": loan_move, "dividends": -div,
                          "net_financing": net_fin}},
        "aged_receivables": {"buckets": buckets,
                             "overdue_total": round(1000.0 + 600.0 + 300.0 + 200.0, 2),
                             "top_overdue": [
                                 {"customer": "Alpha Co", "amount": 400.0, "days": 20},
                                 {"customer": "Beta Co", "amount": 150.0, "days": 95}]},
        "flows": {"capex": capex, "depreciation_ppe": dep, "amortisation": amort,
                  "dividends": div, "other_non_current_assets_movement": onca_move},
        "xero_controls": {"current_year_earnings": ytd["npat"]},
        "disclosed_differences": (
            [{"check": "aged_ar_control", "amount": ar_gap,
              "note": "unapplied credits held in suspense per client bookkeeper"}]
            if ar_gap else []),
    }


# ── debtors fixtures ─────────────────────────────────────────────────────────

DETAIL_HEADER = ["Invoice Number", "Invoice Date", "Due Date", "Invoice Reference",
                 "Current", "< 1 Month", "1 Month", "2 Months", "3 Months", "Older",
                 "Total"]
_BAND_COL = {"current": 4, "lt1": 5, "m1": 6, "m2": 7, "m3": 8, "older": 9}


def detail_row(number: str, due: str, amount: float, band_key: str) -> list:
    row = [number, due, due, "", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, amount]
    row[_BAND_COL[band_key]] = amount
    return row


def make_detail_doc(tmp_path, as_at: str, invoice_rows: list[list],
                    name: str = "detail.json") -> str:
    """A read-ledger-shaped JSON document for an Aged Receivables Detail export."""
    rows = [["Aged Receivables Detail"], ["Fixture Pty Ltd"], [f"As at {as_at}"],
            ["Ageing by due date"], DETAIL_HEADER, ["Customer One Pty Ltd"]]
    rows += invoice_rows
    total = round(sum(r[10] for r in invoice_rows), 2)
    rows.append(["Total", None, None, None, None, None, None, None, None, None, total])
    doc = {"source_file": "synthetic", "metadata": {
               "entity": "Fixture Pty Ltd", "report_title": "Aged Receivables Detail",
               "report_date_raw": f"As at {as_at}", "report_date": as_at,
               "report_period": None},
           "sheets": [{"name": "Aged Receivables Detail", "rows": rows,
                       "formula_count": 1, "formula_cells": [], "merged_cells": 0,
                       "n_rows": len(rows), "n_cols": 11}]}
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def bands_of(invoices: list[tuple[float, str]]) -> dict:
    b = {"current": 0.0, "lt1": 0.0, "m1": 0.0, "m2": 0.0, "m3": 0.0, "older": 0.0}
    for amount, key in invoices:
        b[key] = round(b[key] + amount, 2)
    return b


# ── year-end fixture ─────────────────────────────────────────────────────────

def make_year_end() -> dict:
    bands = {"current": 4000.0, "lt1": 1200.0, "m1": 800.0, "m2": 500.0,
             "m3": 300.0, "older": 700.0}
    total = sum(bands.values())
    return {
        "as_at": "2026-06-30",
        "ar": {"detail_total": total, "summary_total": total, "bs_line": total},
        "prior_year_pnl": {
            "basis": "post_tax", "lodged_basis": "post_tax",
            "lines": [{"name": "revenue", "ours": 180000.0, "lodged": 180000.0},
                      {"name": "npat", "ours": 21000.0, "lodged": 21000.0}]},
        "cash": {"opening_bank": 3000.0, "closing_bank": 4500.0,
                 "movement_stated": 1500.0},
        "ageing_bands": {**bands, "total": total},
        "disclosed_differences": [],
    }
