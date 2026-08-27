"""year_end: the five ties, the six-band shape, the basis edge, the client-disagrees
path; plus the assert mode."""

import json

import pytest

from reconcile_fixtures import make_year_end


@pytest.fixture
def run_year_end(run_cli, tmp_path):
    def _run(data: dict):
        p = tmp_path / "year_end.json"
        p.write_text(json.dumps(data))
        return run_cli(["reconcile", "year_end", "--data", str(p)])
    return _run


def test_five_ties_pass(run_year_end):
    code, env = run_year_end(make_year_end())
    assert code == 0, env["problems"]


@pytest.mark.parametrize("mutate,expect_id", [
    (lambda d: d["ar"].__setitem__("summary_total", 1.0), "ar.detail_vs_summary"),
    (lambda d: d["ar"].__setitem__("bs_line", 1.0), "ar.summary_vs_bs"),
    (lambda d: d["cash"].__setitem__("movement_stated", 1.0), "cash.movement"),
    (lambda d: d["ageing_bands"].__setitem__("older", 1.0), "ageing.bands_total"),
    (lambda d: d["prior_year_pnl"]["lines"][1].__setitem__("lodged", 1.0),
     "prior_year.npat"),
])
def test_each_tie_red(run_year_end, mutate, expect_id):
    d = make_year_end()
    mutate(d)
    code, env = run_year_end(d)
    assert code == 1
    assert any(b["id"] == expect_id for b in env["data"]["breaks"])


def test_five_band_document_is_schema_refusal(run_year_end):
    d = make_year_end()
    # the FY26 collapse: five bands, every value one label younger
    del d["ageing_bands"]["lt1"]
    code, env = run_year_end(d)
    assert code == 2
    assert env["problems"][0]["code"] == "CONTRACT_INVALID"


def test_seven_band_document_is_schema_refusal(run_year_end):
    d = make_year_end()
    d["ageing_bands"]["extra_band"] = 1.0
    code, env = run_year_end(d)
    assert code == 2


def test_pre_tax_against_post_tax_refuses_the_comparison(run_year_end):
    d = make_year_end()
    d["prior_year_pnl"]["basis"] = "pre_tax"       # ours pre-tax, lodged post-tax
    code, env = run_year_end(d)
    assert code == 1
    brk = next(b for b in env["data"]["breaks"] if b["id"] == "prior_year.basis")
    assert "pre_tax" in brk["detail"] and "post_tax" in brk["detail"]
    # and no per-line comparison ran on the unlike bases
    assert not any(c["id"].startswith("prior_year.revenue")
                   for c in env["data"]["checks"])


def test_lodged_gap_passes_as_a_disclosed_finding(run_year_end):
    d = make_year_end()
    d["prior_year_pnl"]["lines"][1]["lodged"] = 21000.0 - 160000.0
    code, env = run_year_end(d)
    assert code == 1                                  # RED first

    d["disclosed_differences"] = [
        {"check": "prior_year.npat", "amount": 160000.0,
         "note": "client's own records disagree with the lodged accounts; "
                 "stated on the page and on the register, June close vs export"}]
    code, env = run_year_end(d)
    assert code == 0, env["problems"]
    applied = env["data"]["disclosures_applied"]
    assert applied[0]["check"] == "prior_year.npat"


# ── assert mode ──────────────────────────────────────────────────────────────

@pytest.fixture
def run_assert(run_cli, tmp_path):
    def _run(data: dict, ties: list):
        dp = tmp_path / "data.json"
        dp.write_text(json.dumps(data))
        tp = tmp_path / "ties.json"
        tp.write_text(json.dumps(ties))
        return run_cli(["reconcile", "assert", "--data", str(dp), "--ties", str(tp)])
    return _run


def test_signed_sum_pass_and_fail(run_assert):
    data = {"a": {"x": 10.0, "y": 2.5}, "b": {"z": 7.5}}
    code, env = run_assert(data, [{"name": "x = y + z",
                                   "left": ["a.x"], "right": ["a.y", "b.z"]}])
    assert code == 0

    code, env = run_assert(data, [{"name": "x - y = z + 1",
                                   "left": ["a.x", "-a.y"], "right": ["b.z", "a.y"]}])
    assert code == 1
    assert env["data"]["breaks"][0]["name"] == "x - y = z + 1"


def test_unknown_path_refuses(run_assert):
    code, env = run_assert({"a": {}}, [{"name": "t", "left": ["a.missing"],
                                        "right": ["a.missing"]}])
    assert code == 2
    assert "a.missing" in env["problems"][0]["message"]


def test_malformed_ties_spec_refuses(run_assert):
    code, env = run_assert({"a": 1.0}, [{"name": "t", "left": []}])
    assert code == 2
