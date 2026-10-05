"""xero group: side by side, summed, and nothing claimed that was not done."""

import json

import openpyxl
import pytest

from xero_fakes import Ledger, Org, signed_in, three_orgs

AS_AT = "2026-06-30"


@pytest.fixture
def pulled(xero, run_cli, tmp_path):
    """Three invented companies, pulled. Returns (pull folder, fake)."""
    three_orgs(xero)
    signed_in(xero, ["t-a", "t-b", "t-c"], keys={"t-a": "A", "t-b": "B", "t-c": "C"})
    out = tmp_path / "pull"
    code, env = run_cli(["xero", "pull", "--as-at", AS_AT, "--all", "--prefix", "ACME",
                         "--out", str(out)])
    assert code == 0, env["problems"]
    return out


def _group(run_cli, pull, out, *extra, as_at=AS_AT):
    return run_cli(["xero", "group", "--pull", str(pull), "--as-at", as_at,
                    "--out", str(out), *extra])


def _table(ws) -> tuple[list, dict]:
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    header = rows[4]
    by_code = {r[0]: dict(zip(header, r)) for r in rows[5:] if r[0]}
    return rows, by_code


def test_the_sheet_says_what_it_is_and_what_it_is_not(pulled, run_cli, tmp_path):
    out = tmp_path / "group.xlsx"
    code, env = _group(run_cli, pulled, out, "--group-name", "ACME group")
    assert code == 0, env["problems"]
    ws = openpyxl.load_workbook(out)["Group"]
    assert ws["A1"].value == "Group trial balance"
    assert ws["A2"].value == ("ACME group: management aggregation of 3 organisations, "
                              "no eliminations, not statutory accounts")
    assert ws["A3"].value == "As at 30 June 2026"
    assert env["data"]["eliminations"] == "none; the column is empty"


def test_each_company_keeps_its_column_and_the_group_is_their_sum(pulled, run_cli, tmp_path):
    out = tmp_path / "group.xlsx"
    code, env = _group(run_cli, pulled, out)
    assert code == 0
    rows, by_code = _table(openpyxl.load_workbook(out)["Group"])
    header = rows[4]
    assert header == ["Code", "Account", "Class", "Type", "Entity A Pty Ltd",
                      "Entity B Pty Ltd", "Entity C Pty Ltd", "Group (sum)",
                      "Eliminations", "Intercompany"]
    bank = by_code["090"]
    assert (bank["Entity A Pty Ltd"], bank["Entity B Pty Ltd"], bank["Entity C Pty Ltd"]) == \
        (5000, 2000, 1000)
    assert bank["Group (sum)"] == 8000
    assert by_code["200"]["Group (sum)"] == -52000        # revenue is a credit
    assert all(r["Eliminations"] is None for r in by_code.values())
    # an account one company does not have is an empty cell, not a zero
    assert by_code["405"]["Entity A Pty Ltd"] is None
    assert by_code["405"]["Entity B Pty Ltd"] == 500
    total = next(r for r in rows if r[1] == "Total (debits less credits)")
    assert total[4:8] == [0, 0, 0, 0], "every trial balance nets to nothing, and so does the sum"
    # classes in statement order
    classes = [r["Class"] for r in by_code.values()]
    assert classes == sorted(classes, key=["ASSET", "LIABILITY", "EQUITY", "REVENUE",
                                           "EXPENSE"].index)


def test_chart_differences_are_listed_never_guessed(pulled, run_cli, tmp_path):
    out = tmp_path / "group.xlsx"
    code, env = _group(run_cli, pulled, out)
    assert code == 0 and env["data"]["differences"] >= 4
    rows = [[c.value for c in r] for r in openpyxl.load_workbook(out)["Chart differences"].iter_rows()]
    found = {(r[0], r[1]): r[3] for r in rows[5:]}
    assert found[("In some organisations only", "405")] == "in B; not in A, C"
    assert found[("In some organisations only", "404")] == "in A; not in B, C"
    assert "A: Advertising; B: Advertising; C: Marketing" == \
        found[("One code, different names", "400")]
    assert ("One name, different codes", "404, 410") in found      # "Bank Fees"
    # and the Group sheet did NOT quietly merge 404 with 410
    _, by_code = _table(openpyxl.load_workbook(out)["Group"])
    assert by_code["404"]["Entity C Pty Ltd"] is None and by_code["410"]["Entity C Pty Ltd"] == 100


def test_a_mapping_lines_codes_up_and_flags_intercompany(pulled, run_cli, tmp_path):
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("entity,code,group_code,group_name,intercompany\n"
                       "C,410,404,Bank Fees,\n"
                       "b,405,404,Bank Fees,no\n"
                       "A,610,610,Receivables,yes\n")
    out = tmp_path / "group.xlsx"
    code, env = _group(run_cli, pulled, out, "--mapping", str(mapping))
    assert code == 0, env["problems"]
    assert env["data"]["mapped"] == 3
    _, by_code = _table(openpyxl.load_workbook(out)["Group"])
    fees = by_code["404"]
    assert (fees["Entity A Pty Ltd"], fees["Entity B Pty Ltd"], fees["Entity C Pty Ltd"]) == \
        (1000, 500, 100)
    assert fees["Group (sum)"] == 1600 and "410" not in by_code and "405" not in by_code
    assert by_code["610"]["Intercompany"] == "yes" and fees["Intercompany"] is None


def test_a_mapping_that_names_an_unknown_company_refuses(pulled, run_cli, tmp_path):
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("entity,code,group_code\nZ,410,404\n")
    code, env = _group(run_cli, pulled, tmp_path / "g.xlsx", "--mapping", str(mapping))
    assert code == 1 and env["problems"][0]["code"] == "MAPPING_UNKNOWN_ENTITY"
    mapping.write_text("company,account\nA,1\n")
    code, env = _group(run_cli, pulled, tmp_path / "g.xlsx", "--mapping", str(mapping))
    assert code == 2 and env["problems"][0]["code"] == "MAPPING_INVALID"


def test_the_group_sheet_passes_the_same_date_gate(pulled, run_cli, tmp_path):
    out = tmp_path / "group.xlsx"
    assert _group(run_cli, pulled, out)[0] == 0
    code, env = run_cli(["read-ledger", str(out), "--expect-date", AS_AT])
    assert code == 0, env["problems"]
    assert env["data"]["metadata"]["report_title"] == "Group trial balance"
    code, env = run_cli(["read-ledger", str(out), "--expect-date", "2026-05-31"])
    assert code == 1


def test_the_date_is_checked_against_the_pull(pulled, run_cli, tmp_path):
    code, env = _group(run_cli, pulled, tmp_path / "g.xlsx", as_at="2026-05-31")
    assert code == 1 and env["problems"][0]["code"] == "DATE_MISMATCH"
    code, env = run_cli(["xero", "group", "--pull", str(pulled), "--out",
                         str(tmp_path / "g.xlsx")])
    assert code == 1 and env["problems"][0]["code"] == "AS_AT_REQUIRED"


def test_a_folder_that_is_not_a_pull_refuses(run_cli, tmp_path):
    code, env = _group(run_cli, tmp_path, tmp_path / "g.xlsx")
    assert code == 1 and env["problems"][0]["code"] == "PULL_RECORD_MISSING"


def test_inputs_changed_since_the_pull_refuse(pulled, run_cli, tmp_path):
    raw = pulled / "raw" / "ACME_B_TrialBalance_2026-06-30.json"
    raw.write_bytes(raw.read_bytes().replace(b"2000.00", b"2999.00"))
    code, env = _group(run_cli, pulled, tmp_path / "g.xlsx")
    assert code == 1 and env["problems"][0]["code"] == "PULL_CHANGED"
    assert not (tmp_path / "g.xlsx").exists()


def test_an_out_of_balance_company_is_not_added_to_anything(pulled, run_cli, tmp_path):
    record_path = pulled / "PULL.json"
    doc = json.loads(record_path.read_text())
    raw = pulled / "raw" / "ACME_B_TrialBalance_2026-06-30.json"
    blob = raw.read_bytes().replace(b'"Value": "500.00"', b'"Value": "590.00"')
    raw.write_bytes(blob)
    # make the record agree with the changed bytes, so only the imbalance is at issue
    import hashlib
    for org in doc["organisations"]:
        for r in org["raw"]:
            if r["file"].endswith("ACME_B_TrialBalance_2026-06-30.json"):
                r["sha256"] = hashlib.sha256(blob).hexdigest()
    record_path.write_text(json.dumps(doc))
    code, env = _group(run_cli, pulled, tmp_path / "g.xlsx")
    assert code == 1 and env["problems"][0]["code"] == "TRIAL_BALANCE_OUT"
    assert "B:" in env["problems"][0]["message"]


def test_different_currencies_cannot_be_added(xero, run_cli, tmp_path):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-n", "Entity N Limited", currency="NZD"))
    signed_in(xero, ["t-a", "t-n"], keys={"t-a": "A", "t-n": "N"})
    pull = tmp_path / "pull"
    assert run_cli(["xero", "pull", "--as-at", AS_AT, "--all", "--out", str(pull)])[0] == 0
    code, env = _group(run_cli, pull, tmp_path / "g.xlsx")
    assert code == 1 and env["problems"][0]["code"] == "CURRENCIES_DIFFER"
    assert "AUD, NZD" in env["problems"][0]["message"]


def test_different_year_ends_are_said_on_the_face(xero, run_cli, tmp_path):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    xero.add_org(Org("t-d", "Entity D Pty Ltd", fy_end=(31, 12)))
    signed_in(xero, ["t-a", "t-d"], keys={"t-a": "A", "t-d": "D"})
    pull = tmp_path / "pull"
    assert run_cli(["xero", "pull", "--as-at", AS_AT, "--all", "--out", str(pull)])[0] == 0
    out = tmp_path / "g.xlsx"
    code, env = _group(run_cli, pull, out)
    assert code == 0
    assert any("different financial-year ends" in w for w in env["warnings"])
    assert "financial-year ends differ" in openpyxl.load_workbook(out)["Group"]["A2"].value


def test_a_pull_without_trial_balances_cannot_be_grouped(xero, run_cli, tmp_path):
    xero.add_org(Org("t-a", "Entity A Pty Ltd"))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    pull = tmp_path / "pull"
    assert run_cli(["xero", "pull", "--as-at", AS_AT, "--all", "--reports", "bs,pl",
                    "--out", str(pull)])[0] == 0
    code, env = _group(run_cli, pull, tmp_path / "g.xlsx")
    assert code == 1 and env["problems"][0]["code"] == "TRIAL_BALANCE_MISSING"


def test_group_uses_no_network(pulled, run_cli, tmp_path, monkeypatch):
    from fma_tools.xero import transport

    def none():
        raise AssertionError("group must not touch the network")
    monkeypatch.setattr(transport, "_FACTORY", none)
    assert _group(run_cli, pulled, tmp_path / "g.xlsx")[0] == 0


def test_one_company_is_still_a_sheet(xero, run_cli, tmp_path):
    xero.add_org(Org("t-a", "Entity A Pty Ltd", ledger=Ledger()))
    signed_in(xero, ["t-a"], keys={"t-a": "A"})
    pull = tmp_path / "pull"
    assert run_cli(["xero", "pull", "--as-at", AS_AT, "--all", "--out", str(pull)])[0] == 0
    out = tmp_path / "g.xlsx"
    code, env = _group(run_cli, pull, out)
    assert code == 0 and env["data"]["differences"] == 0
    about = [[c.value for c in r] for r in openpyxl.load_workbook(out)["About"].iter_rows()]
    assert about[4][:3] == ["Key", "Legal entity", "Xero name"]
    assert about[5][0] == "A" and about[5][5] == "adviser@example.test"


def test_without_the_chart_a_missing_account_is_not_claimed_to_be_missing(xero, run_cli, tmp_path):
    three_orgs(xero)
    signed_in(xero, ["t-a", "t-b", "t-c"], keys={"t-a": "A", "t-b": "B", "t-c": "C"})
    pull = tmp_path / "pull"
    assert run_cli(["xero", "pull", "--as-at", AS_AT, "--all", "--reports", "tb",
                    "--out", str(pull)])[0] == 0
    out = tmp_path / "g.xlsx"
    code, env = _group(run_cli, pull, out)
    assert code == 0
    assert any("judged from accounts with a balance" in w for w in env["warnings"])
    rows = [[c.value for c in r] for r in openpyxl.load_workbook(out)["Chart differences"].iter_rows()]
    assert rows[5][0] == "Read this first" and "may only be unused" in rows[5][3]
    # class and code still come through, from the trial balance's own headings and labels
    _, by_code = _table(openpyxl.load_workbook(out)["Group"])
    assert by_code["090"]["Class"] == "ASSET" and by_code["200"]["Class"] == "REVENUE"
    assert by_code["400"]["Class"] == "EXPENSE" and by_code["800"]["Class"] == "LIABILITY"
