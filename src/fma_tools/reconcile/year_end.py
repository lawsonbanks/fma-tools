"""Year-end ties — the five that gate the room pack before a page is written.

The refusal edge lives with the agent, not here: if a tie breaks because WE read the
source wrong, stop and do not render a page (this tool's exit 1 is that trigger). If
it breaks because the CLIENT'S OWN records disagree, that is a finding — recorded as
a disclosed difference with a note, stated on the page, and the build continues. What
never ships is a number we cannot trace.
"""

from __future__ import annotations

from .lib import DOLLAR, CheckResult, Disclosures, fact, tie

BAND_KEYS = ("current", "lt1", "m1", "m2", "m3", "older")


def checks(data: dict, disc: Disclosures) -> list[CheckResult]:
    out: list[CheckResult] = []

    # 1. AR detail = AR summary = the Accounts Receivable line on the Balance Sheet
    ar = data["ar"]
    out.append(tie("ar.detail_vs_summary", "AR Detail total = AR Summary total",
                   ar["detail_total"], ar["summary_total"],
                   disclosed=disc.for_check("ar.detail_vs_summary")))
    out.append(tie("ar.summary_vs_bs", "AR Summary total = Balance Sheet AR line",
                   ar["summary_total"], ar["bs_line"],
                   disclosed=disc.for_check("ar.summary_vs_bs")))

    # 2. prior-year P&L vs the lodged statutory accounts, line by line, to the
    # dollar. The canonical client-records-disagree site: a lodged-accounts gap is a
    # disclosed finding per line, never a silent pass.
    py = data["prior_year_pnl"]
    if py["basis"] != py["lodged_basis"]:
        # Never set a pre-tax figure against a post-tax one. FY26 nearly shipped
        # +62.5% profit growth on that mix; stated properly the margin had fallen.
        out.append(fact("prior_year.basis", "prior-year bases match "
                        "(never pre-tax against post-tax)", False,
                        f"ours {py['basis']!r} vs lodged {py['lodged_basis']!r}"))
    else:
        out.append(fact("prior_year.basis", "prior-year bases match", True,
                        py["basis"]))
        for line in py["lines"]:
            cid = f"prior_year.{line['name']}"
            out.append(tie(cid, f"prior-year {line['name']}: ours = lodged",
                           line["ours"], line["lodged"], tol=DOLLAR,
                           disclosed=disc.for_check(cid)))

    # 3. movement in cash = closing bank less opening bank
    cash = data["cash"]
    out.append(tie("cash.movement", "cash movement = closing bank - opening bank",
                   cash["movement_stated"], cash["closing_bank"] - cash["opening_bank"],
                   disclosed=disc.for_check("cash.movement")))

    # 4. the ageing reads as SIX bands and they sum to the total. The six-key shape
    # is enforced by the schema, so a five-band figure set is an input refusal
    # before this runs — FY26 collapsed six to five and every value landed one
    # label younger, printing Current at $28k against a true $1,988,929.
    bands = data["ageing_bands"]
    out.append(tie("ageing.bands_total", "six ageing bands sum to the total",
                   sum(bands[k] for k in BAND_KEYS), bands["total"]))

    return out
