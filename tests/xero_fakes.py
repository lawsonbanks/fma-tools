"""A Xero that fits in memory, for tests. Every organisation here is invented.

`FakeXero` is a transport: it answers the token endpoint, the connections list and the
handful of accounting endpoints `fma xero` calls, in the JSON shapes Xero documents. It
also has knobs for the ways the real thing misbehaves, because those are the tests that
matter: a title that echoes the wrong date, a total that does not foot, a refresh token
already spent, a 429, a missing permission.

Tokens are built at run time (never committed) and remembered in `issued`, so a test
can assert that none of them leaked into any output.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from fma_tools.xero import oauth
from fma_tools.xero.reports import financial_year, long_date
from fma_tools.xero.transport import Response, TransportError

API_PREFIX = "https://api.xero.com/api.xro/2.0/"
CONNECTIONS = "https://api.xero.com/connections"


def jwt(claims: dict) -> str:
    def part(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part(claims)}.{part({'sig': len(claims)})}"


def D(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


@dataclass
class Ledger:
    """A tiny set of books that balances by construction: retained earnings is the plug."""
    bank: list = field(default_factory=lambda: [("090", "Business Bank Account", 5000)])
    current_assets: list = field(default_factory=lambda: [("610", "Accounts Receivable", 12000)])
    current_liabilities: list = field(default_factory=lambda: [("800", "Accounts Payable", 3000)])
    income: list = field(default_factory=lambda: [("200", "Sales", 40000)])
    expenses: list = field(default_factory=lambda: [("400", "Advertising", 6000),
                                                    ("404", "Bank Fees", 1000),
                                                    ("477", "Wages and Salaries", 21000)])

    def total(self, name: str) -> Decimal:
        return sum((D(a) for _, _, a in getattr(self, name)), Decimal(0))

    @property
    def net_profit(self) -> Decimal:
        return self.total("income") - self.total("expenses")

    @property
    def net_assets(self) -> Decimal:
        return (self.total("bank") + self.total("current_assets")
                - self.total("current_liabilities"))

    @property
    def retained(self) -> Decimal:
        return self.net_assets - self.net_profit


@dataclass
class Org:
    tenant_id: str
    name: str
    legal_name: str = ""
    currency: str = "AUD"
    fy_end: tuple = (30, 6)                 # day, month
    ledger: Ledger = field(default_factory=Ledger)
    at: dict = field(default_factory=dict)  # iso date -> Ledger, for an earlier date
    archived: list = field(default_factory=lambda: [("999", "Old Suspense")])
    archive_codes: set = field(default_factory=set)   # ledger codes Xero marks ARCHIVED

    def books(self, when: str) -> Ledger:
        return self.at.get(when, self.ledger)

    def account_id(self, code: str, name: str = "") -> str:
        """Xero's id for an account; a code-less one (a bank account, often) is told
        apart by its name."""
        return f"acct-{self.tenant_id}-{code or 'nocode-' + name}"


def _cell(value, account_id=None) -> dict:
    c = {"Value": value if isinstance(value, str) else f"{value:.2f}"}
    if account_id:
        c["Attributes"] = [{"Value": account_id, "Id": "account"}]
    return c


def _row(label, values, account_id=None, kind="Row") -> dict:
    return {"RowType": kind,
            "Cells": [_cell(label, account_id)] + [_cell(v, account_id) for v in values]}


def _section(title, rows) -> dict:
    return {"RowType": "Section", "Title": title, "Rows": rows}


def _report(titles, header, sections) -> bytes:
    return json.dumps({"Reports": [{
        "ReportID": titles[0].replace(" ", ""), "ReportName": titles[0],
        "ReportTitles": titles,
        "ReportDate": "1 January 2001",          # the run date; never the as-at
        "Rows": [{"RowType": "Header", "Cells": [{"Value": h} for h in header]}] + sections,
    }]}).encode()


def _short(d: date) -> str:
    return d.strftime("%d %b %Y").lstrip("0")


@dataclass
class FakeXero:
    orgs: dict = field(default_factory=dict)           # tenant id -> Org
    users: dict = field(default_factory=dict)          # user id -> {"email", "connections"}
    log: list = field(default_factory=list)            # (method, url, headers)
    issued: list = field(default_factory=list)         # every token string handed out
    access: dict = field(default_factory=dict)         # access token -> user id
    refresh_tokens: dict = field(default_factory=dict)  # live refresh token -> user id
    next_consent: dict | None = None
    expected_challenge: str | None = None
    good_code: str = "good-code"
    # knobs
    transport_down: bool = False
    token_status: int | None = None                    # force the token endpoint's status
    token_error: str = "invalid_grant"
    rate_limit: list = field(default_factory=list)     # headers for 429s, one per call
    api_status: tuple | None = None                    # force (status, body) on API calls
    delete_status: int | None = None                   # force the answer to a DELETE
    event_filter_blind: bool = False                   # ?authEventId= matches nothing
    connections_status: int | None = None              # force the answer to GET /connections
    no_scope_for: set = field(default_factory=set)     # path fragments answered 401
    # Both may carry a date as a last element, to bend one date of a comparison only.
    skew_title: dict = field(default_factory=dict)     # (tenant, report) -> title line
    bend_total: dict = field(default_factory=dict)     # (tenant, report, label) -> delta
    text_cell: dict = field(default_factory=dict)      # (tenant, "bs", label) -> cell text
    drop_from_token: set = field(default_factory=set)  # fields left out of a new sign-in
    grant_without: set = field(default_factory=set)    # scopes Xero quietly does not grant
    one_sided: dict = field(default_factory=dict)      # tenant -> a debit with no credit
    day_remaining: int | None = 995
    before_api: object = None                          # called before each API answer
    _n: int = 0

    # -- setting the scene -----------------------------------------------------------

    def add_org(self, org: Org) -> Org:
        self.orgs[org.tenant_id] = org
        return org

    def will_grant(self, user_id: str, email: str, tenants: list[str], event: str = None):
        self._n += 1
        self.next_consent = {"user_id": user_id, "email": email, "tenants": list(tenants),
                             "event": event or f"event-{self._n}"}

    def _issue(self, user_id: str, event: str, email: str) -> dict:
        self._n += 1
        access = jwt({"xero_userid": user_id, "authentication_event_id": event,
                      "scope": list(oauth.SCOPES), "n": self._n})
        refresh = f"refresh-{self._n}-{'r' * 24}"
        self.access[access] = user_id
        self.refresh_tokens[refresh] = user_id
        self.issued += [access, refresh]
        granted = [s for s in oauth.SCOPES if s not in self.grant_without]
        tok = {"access_token": access, "refresh_token": refresh, "expires_in": 1800,
               "token_type": "Bearer", "scope": " ".join(granted),
               "id_token": jwt({"email": email, "xero_userid": user_id})}
        return {k: v for k, v in tok.items() if k not in self.drop_from_token}

    # -- the transport ---------------------------------------------------------------

    def request(self, method, url, headers, data=None, timeout=30) -> Response:
        self.log.append((method, url, dict(headers)))
        if self.transport_down:
            raise TransportError("URLError")
        if url == oauth.TOKEN_URL:
            return self._token(urllib.parse.parse_qs((data or b"").decode()))
        token = (headers.get("Authorization") or "").removeprefix("Bearer ")
        user_id = self.access.get(token)
        if user_id is None:
            return Response(401, {}, b'{"Title":"Unauthorized"}')
        if url.startswith(CONNECTIONS):
            return self._connections(method, url, user_id)
        if url.startswith(API_PREFIX):
            return self._api(url, headers, user_id)
        return Response(404, {}, b"{}")

    def _json(self, status, obj, headers=None) -> Response:
        return Response(status, headers or {}, json.dumps(obj).encode())

    def _token(self, form) -> Response:
        one = lambda k: (form.get(k) or [""])[0]      # noqa: E731
        if self.token_status is not None:
            return self._json(self.token_status, {"error": self.token_error})
        if one("grant_type") == "authorization_code":
            c = self.next_consent
            ok = (c and one("code") == self.good_code and one("client_id")
                  and (self.expected_challenge is None
                       or oauth.challenge_for(one("code_verifier")) == self.expected_challenge))
            if not ok:
                return self._json(400, {"error": "invalid_grant"})
            user = self.users.setdefault(c["user_id"], {"email": c["email"], "connections": []})
            for tid in c["tenants"]:
                org = self.orgs[tid]
                user["connections"] = [x for x in user["connections"] if x["tenantId"] != tid]
                user["connections"].append({
                    "id": f"conn-{c['user_id']}-{tid}", "authEventId": c["event"],
                    "tenantId": tid, "tenantType": "ORGANISATION", "tenantName": org.name,
                    "createdDateUtc": "2026-01-02T03:04:05.0000000"})
            self.next_consent = None
            return self._json(200, self._issue(c["user_id"], c["event"], c["email"]))
        if one("grant_type") == "refresh_token":
            user_id = self.refresh_tokens.pop(one("refresh_token"), None)   # spent once
            if user_id is None:
                return self._json(400, {"error": "invalid_grant"})
            return self._json(200, self._issue(user_id, "event-refresh",
                                               self.users[user_id]["email"]))
        return self._json(400, {"error": "unsupported_grant_type"})

    def _connections(self, method, url, user_id) -> Response:
        user = self.users[user_id]
        parsed = urllib.parse.urlparse(url)
        if method == "DELETE":
            if self.delete_status is not None:
                return Response(self.delete_status, {}, b'{"Title":"Forbidden"}')
            conn_id = parsed.path.rsplit("/", 1)[-1]
            user["connections"] = [c for c in user["connections"] if c["id"] != conn_id]
            return Response(204, {}, b"")
        if self.connections_status is not None:
            return Response(self.connections_status, {}, b"{}")
        event = (urllib.parse.parse_qs(parsed.query).get("authEventId") or [None])[0]
        if event is not None and self.event_filter_blind:
            return self._json(200, [])
        rows = [c for c in user["connections"] if event is None or c["authEventId"] == event]
        return self._json(200, rows)

    def _api(self, url, headers, user_id) -> Response:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path[len("/api.xro/2.0/"):]
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        tenant = headers.get("Xero-Tenant-Id")
        if self.before_api:
            self.before_api(path, tenant)
        if self.rate_limit:
            return Response(429, self.rate_limit.pop(0), b"{}")
        if self.api_status:
            return Response(self.api_status[0], {}, self.api_status[1])
        if any(frag in path for frag in self.no_scope_for):
            return Response(401, {"www-authenticate": 'Bearer error="insufficient_scope"'},
                            b"{}")
        if tenant not in {c["tenantId"] for c in self.users[user_id]["connections"]}:
            return Response(403, {}, b'{"Title":"Forbidden","Detail":"AuthenticationUnsuccessful"}')
        org = self.orgs[tenant]
        limits = {}
        if self.day_remaining is not None:
            self.day_remaining -= 1
            limits = {"x-daylimit-remaining": str(self.day_remaining),
                      "x-minlimit-remaining": "59"}
        body = self._body(org, path, q)
        if body is None:
            return Response(404, limits, b'{"Title":"Not found"}')
        return Response(200, limits, body)

    # -- the books -------------------------------------------------------------------

    def _bent(self, org, report, label, amount: Decimal, when: str | None = None) -> Decimal:
        delta = self.bend_total.get((org.tenant_id, report, label, when),
                                    self.bend_total.get((org.tenant_id, report, label), 0))
        return amount + D(delta)

    def _titles(self, org, report, name, line, when: str | None = None) -> list:
        said = self.skew_title.get((org.tenant_id, report, when),
                                   self.skew_title.get((org.tenant_id, report), line))
        return [name, org.name, said]

    def _body(self, org: Org, path: str, q: dict) -> bytes | None:
        if path == "Organisation":
            return json.dumps({"Organisations": [{
                "Name": org.name, "LegalName": org.legal_name or org.name,
                "BaseCurrency": org.currency, "FinancialYearEndDay": org.fy_end[0],
                "FinancialYearEndMonth": org.fy_end[1], "SalesTaxBasis": "ACCRUALS",
                "Timezone": "AUSEASTERNSTANDARDTIME", "IsDemoCompany": False}]}).encode()
        if path == "Accounts":
            b = org.ledger
            rows = []
            for group, typ, cls in (("bank", "BANK", "ASSET"), ("current_assets", "CURRENT", "ASSET"),
                                    ("current_liabilities", "CURRLIAB", "LIABILITY"),
                                    ("income", "REVENUE", "REVENUE"),
                                    ("expenses", "EXPENSE", "EXPENSE")):
                rows += [{"AccountID": org.account_id(c, n), "Code": c, "Name": n, "Type": typ,
                          "Class": cls, "TaxType": "NONE",
                          "Status": "ARCHIVED" if c in org.archive_codes else "ACTIVE"}
                         for c, n, _ in getattr(b, group)]
            rows.append({"AccountID": org.account_id("960"), "Code": "960",
                         "Name": "Retained Earnings", "Type": "EQUITY", "Class": "EQUITY",
                         "Status": "ACTIVE", "TaxType": "NONE"})
            rows += [{"AccountID": org.account_id(c), "Code": c, "Name": n, "Type": "EXPENSE",
                      "Class": "EXPENSE", "Status": "ARCHIVED", "TaxType": "NONE"}
                     for c, n in org.archived if c not in org.archive_codes]
            return json.dumps({"Accounts": rows}).encode()
        if path == "Reports/TrialBalance":
            return self._trial_balance(org, q["date"])
        if path == "Reports/BalanceSheet":
            return self._balance_sheet(org, q["date"])
        if path == "Reports/ProfitAndLoss":
            return self._profit_and_loss(org, q["fromDate"], q["toDate"])
        if path == "Reports/BankSummary":
            return self._bank_summary(org, q["fromDate"], q["toDate"])
        return None

    def _trial_balance(self, org, when) -> bytes:
        b = org.books(when)
        aid = org.account_id

        def rows(group, debit: bool):
            out = []
            for c, n, a in getattr(b, group):
                dr, cr = (D(a), "") if debit else ("", D(a))
                out.append(_row(f"{n} ({c})" if c else n, [dr, cr, dr, cr], aid(c, n)))
            return out
        retained = b.retained
        eq = _row("Retained Earnings (960)", ["", retained, "", retained], aid("960"))
        debits = b.total("bank") + b.total("current_assets") + b.total("expenses")
        total = self._bent(org, "tb", "Total", debits)
        stray = self.one_sided.get(org.tenant_id)
        if stray:
            debits += D(stray)
            total += D(stray)
        sections = [
            _section("Revenue", rows("income", False)),
            _section("Expenses", rows("expenses", True) + (
                [_row("Suspense (998)", [D(stray), "", D(stray), ""], aid("998"))]
                if stray else [])),
            _section("Assets", rows("bank", True) + rows("current_assets", True)),
            _section("Liabilities", rows("current_liabilities", False)),
            _section("Equity", [eq]),
            _section("", [_row("Total", [total, debits - D(stray or 0), total,
                                        debits - D(stray or 0)], kind="SummaryRow")]),
        ]
        line = f"As at {long_date(date.fromisoformat(when))}"
        return _report(self._titles(org, "tb", "Trial Balance", line),
                       ["Account", "Debit", "Credit", "YTD Debit", "YTD Credit"], sections)

    def _balance_sheet(self, org, when) -> bytes:
        b = org.books(when)
        aid = org.account_id

        def rows(group):
            return [_row(n, [self.text_cell.get((org.tenant_id, "bs", n), D(a))], aid(c, n))
                    for c, n, a in getattr(b, group)]
        assets = b.total("bank") + b.total("current_assets")
        cye = self._bent(org, "bs", "Current Year Earnings", b.net_profit, when)
        sections = [
            _section("Assets", []),
            _section("Bank", rows("bank") + [
                _row("Total Bank", [self._bent(org, "bs", "Total Bank", b.total("bank"), when)],
                     kind="SummaryRow")]),
            _section("Current Assets", rows("current_assets") + [
                _row("Total Current Assets", [b.total("current_assets")], kind="SummaryRow")]),
            _section("", [_row("Total Assets", [assets], kind="SummaryRow")]),
            _section("Liabilities", []),
            _section("Current Liabilities", rows("current_liabilities") + [
                _row("Total Current Liabilities", [b.total("current_liabilities")],
                     kind="SummaryRow")]),
            _section("", [_row("Total Liabilities", [b.total("current_liabilities")],
                               kind="SummaryRow")]),
            _section("", [_row("Net Assets", [self._bent(org, "bs", "Net Assets", b.net_assets,
                                                         when)])]),
            _section("Equity", [
                _row("Current Year Earnings", [cye]),
                _row("Retained Earnings", [b.retained], aid("960")),
                _row("Total Equity", [cye + b.retained], kind="SummaryRow")]),
        ]
        d = date.fromisoformat(when)
        return _report(self._titles(org, "bs", "Balance Sheet", f"As at {long_date(d)}", when),
                       ["", _short(d)], sections)

    def _profit_and_loss(self, org, start, end) -> bytes:
        a, b_ = date.fromisoformat(start), date.fromisoformat(end)
        books = org.books(end)
        fy_start, _ = financial_year(b_, org.fy_end[1], org.fy_end[0])
        # the year to date carries the ledger's own figures; any other period a third
        share = Decimal(1) if a == fy_start else Decimal(3)
        aid = org.account_id

        def rows(group):
            return [(c, n, (D(amt) / share).quantize(Decimal("0.01")))
                    for c, n, amt in getattr(books, group)]
        inc, exp = rows("income"), rows("expenses")
        ti = sum((v for _, _, v in inc), Decimal(0))
        te = sum((v for _, _, v in exp), Decimal(0))
        sections = [
            _section("Income", [_row(n, [v], aid(c, n)) for c, n, v in inc] + [
                _row("Total Income", [self._bent(org, "pl", "Total Income", ti)],
                     kind="SummaryRow")]),
            _section("", [_row("Gross Profit", [ti])]),
            _section("Less Operating Expenses", [_row(n, [v], aid(c, n)) for c, n, v in exp] + [
                _row("Total Operating Expenses", [te], kind="SummaryRow")]),
            _section("", [_row("Net Profit", [self._bent(org, "pl", "Net Profit", ti - te)])]),
        ]
        line = f"{long_date(a)} to {long_date(b_)}"
        return _report(self._titles(org, "pl", "Profit & Loss", line), ["", _short(b_)], sections)

    def _bank_summary(self, org, start, end) -> bytes:
        a, b_ = date.fromisoformat(start), date.fromisoformat(end)
        books = org.books(end)
        rows, closing = [], Decimal(0)
        for c, n, amt in books.bank:
            rows.append(_row(n, [D(amt) - 1000, D(3000), D(2000), D(amt)], org.account_id(c, n)))
            closing += D(amt)
        n = len(books.bank)
        rows.append(_row("Total", [closing - 1000 * n, D(3000 * n), D(2000 * n),
                                   self._bent(org, "bank", "Total", closing)],
                         kind="SummaryRow"))
        line = f"From {long_date(a)} to {long_date(b_)}"
        return _report(self._titles(org, "bank", "Bank Summary", line),
                       ["Bank Accounts", "Opening Balance", "Cash Received", "Cash Spent",
                        "Closing Balance"], [_section("", rows)])

    # -- what tests ask afterwards ---------------------------------------------------

    def api_calls(self, tenant: str | None = None) -> list:
        return [(m, u, h) for m, u, h in self.log
                if u.startswith(API_PREFIX) and (tenant is None or h.get("Xero-Tenant-Id") == tenant)]


def three_orgs(fake: FakeXero) -> list[Org]:
    """Three companies of one invented group whose charts have drifted apart, the way
    real ones do."""
    a = fake.add_org(Org("t-a", "Entity A Pty Ltd"))
    b = fake.add_org(Org("t-b", "Entity B Pty Ltd", ledger=Ledger(
        bank=[("090", "Business Bank Account", 2000)],
        current_assets=[("610", "Accounts Receivable", 4000)],
        current_liabilities=[("800", "Accounts Payable", 1000)],
        income=[("200", "Sales", 9000)],
        # no 404; a 405 the others do not have
        expenses=[("400", "Advertising", 2000), ("405", "Bank Charges", 500)])))
    c = fake.add_org(Org("t-c", "Entity C Pty Ltd", ledger=Ledger(
        bank=[("090", "Business Bank Account", 1000)],
        current_assets=[("610", "Accounts Receivable", 500)],
        current_liabilities=[("800", "Accounts Payable", 250)],
        income=[("200", "Sales", 3000)],
        # 400 under another name; "Bank Fees" under another code
        expenses=[("400", "Marketing", 1000), ("410", "Bank Fees", 100)])))
    return [a, b, c]


def signed_in(fake: FakeXero, tenant_ids: list[str], keys: dict | None = None,
              user_id: str = "user-1", email: str = "adviser@example.test",
              expired: bool = False, now: float | None = None) -> dict:
    """Put this Mac (the test's temp config folder) in the state a finished `auth`
    leaves it in, without going through a browser. Returns the token set issued."""
    import time
    from fma_tools.xero import store, tenants
    now = time.time() if now is None else now
    fake.will_grant(user_id, email, tenant_ids)
    event = fake.next_consent["event"]
    user = fake.users.setdefault(user_id, {"email": email, "connections": []})
    for tid in tenant_ids:
        user["connections"] = [c for c in user["connections"] if c["tenantId"] != tid]
        user["connections"].append({"id": f"conn-{user_id}-{tid}", "authEventId": event,
                                    "tenantId": tid, "tenantType": "ORGANISATION",
                                    "tenantName": fake.orgs[tid].name,
                                    "createdDateUtc": "2026-01-02T03:04:05.0000000"})
    fake.next_consent = None
    tok = fake._issue(user_id, event, email)
    if not store.load(store.APP):
        store.save(store.APP, {"client_id": "A" * 32, "port": 8976})
    tokens = store.load(store.TOKENS)
    stamped = oauth.stamp(tok, now)
    if expired:
        stamped["access_expires_at"] = now - 10
    tokens.setdefault("users", {})[user_id] = {**stamped, "email": email}
    store.save(store.TOKENS, tokens)
    rows = tenants.registry()
    for tid in tenant_ids:
        rows[tid] = {"key": (keys or {}).get(tid), "name": fake.orgs[tid].name,
                     "type": "ORGANISATION", "connection_id": f"conn-{user_id}-{tid}",
                     "user_id": user_id, "authorised_by": email, "auth_event_id": event,
                     "connected_at": "2026-01-02T03:04:05.0000000",
                     "saved_at": "2026-01-02T03:04:06+00:00"}
    tenants.save_registry(rows)
    return tok
