"""Your agent: a question in, a structured answer out."""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from agents.llm import complete_json

OUTPUT = Path(__file__).resolve().parents[1] / "output"
FILINGS = OUTPUT / "filings.parquet"
HOLDINGS = OUTPUT / "holdings.parquet"

VALID_UNITS = {"USD", "SHARES", "COUNT", "PERCENT", "NAME", "DATE", "NONE"}
ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")

_FILINGS_CACHE = None
_HOLDINGS_CACHE = None
_ISSUER_NAMES_CACHE = None


def _load_filings():
    global _FILINGS_CACHE
    if _FILINGS_CACHE is None:
        _FILINGS_CACHE = pq.read_table(FILINGS).to_pylist()
    return _FILINGS_CACHE


def _load_holdings():
    global _HOLDINGS_CACHE
    if _HOLDINGS_CACHE is None:
        _HOLDINGS_CACHE = pq.read_table(HOLDINGS).to_pylist()
    return _HOLDINGS_CACHE


def _normalize_quarter(s):
    if not s:
        return None
    m = re.search(r"(20\d{2}).{0,3}Q\s*([1-4])", s, re.IGNORECASE)
    if m:
        return f"{m.group(1)}Q{m.group(2)}"
    m = re.search(r"Q\s*([1-4]).{0,3}(20\d{2})", s, re.IGNORECASE)
    if m:
        return f"{m.group(2)}Q{m.group(1)}"
    return None


def _resolve_manager(query, filings):
    if not query:
        return None
    names = sorted({f["fund_name"] for f in filings})
    q = query.strip().lower()
    for name in names:
        if q == name.lower():
            return name
    for name in names:
        if q in name.lower() or name.lower() in q:
            return name
    matches = difflib.get_close_matches(query, names, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _resolve_issuer(query, holdings):
    if not query:
        return None
    global _ISSUER_NAMES_CACHE
    if _ISSUER_NAMES_CACHE is None:
        _ISSUER_NAMES_CACHE = sorted({h["name_of_issuer"] for h in holdings})
    names = _ISSUER_NAMES_CACHE
    q = query.strip().lower()
    for name in names:
        if q == name.lower():
            return name
    candidates = [n for n in names if q in n.lower()]
    if candidates:
        return min(candidates, key=len)
    matches = difflib.get_close_matches(query, names, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _op_largest_position_by_issuer(plan, filings, holdings):
    issuer = _resolve_issuer(plan.get("issuer_query"), holdings)
    quarter = _normalize_quarter(plan.get("quarter"))
    if not issuer or not quarter:
        return None
    rows = [h for h in holdings if h["name_of_issuer"] == issuer and h["report_quarter"] == quarter]
    if not rows:
        return None
    best = max(rows, key=lambda r: r["value"] or 0)
    cik_to_name = {f["cik"]: f["fund_name"] for f in filings}
    manager = cik_to_name.get(best["cik"])
    if manager is None:
        return None
    return manager, "NAME", [best["accession_number"]]


def _op_issuer_change_between_quarters(plan, filings, holdings):
    issuer = _resolve_issuer(plan.get("issuer_query"), holdings)
    q_from = _normalize_quarter(plan.get("quarter_from"))
    q_to = _normalize_quarter(plan.get("quarter_to"))
    if not issuer or not q_from or not q_to:
        return None
    from_rows = [h for h in holdings if h["name_of_issuer"] == issuer and h["report_quarter"] == q_from]
    to_rows = [h for h in holdings if h["name_of_issuer"] == issuer and h["report_quarter"] == q_to]
    ciks = {r["cik"] for r in from_rows} | {r["cik"] for r in to_rows}
    if not ciks:
        return None
    best_cik, best_delta, best_sources = None, None, []
    for cik in sorted(ciks):
        from_shares = sum(r["ssh_prnamt"] or 0 for r in from_rows if r["cik"] == cik)
        to_shares = sum(r["ssh_prnamt"] or 0 for r in to_rows if r["cik"] == cik)
        delta = to_shares - from_shares
        if best_delta is None or delta > best_delta:
            best_delta, best_cik = delta, cik
            best_sources = sorted(
                {r["accession_number"] for r in from_rows if r["cik"] == cik}
                | {r["accession_number"] for r in to_rows if r["cik"] == cik}
            )
    cik_to_name = {f["cik"]: f["fund_name"] for f in filings}
    manager = cik_to_name.get(best_cik)
    if manager is None:
        return None
    return manager, "NAME", best_sources


def _op_distinct_issuer_count(plan, filings, holdings):
    manager = _resolve_manager(plan.get("manager_name"), filings)
    quarter = _normalize_quarter(plan.get("quarter"))
    if not manager or not quarter:
        return None
    cik = next((f["cik"] for f in filings if f["fund_name"] == manager), None)
    accession = next(
        (f["accession_number"] for f in filings if f["fund_name"] == manager and f["report_quarter"] == quarter),
        None,
    )
    if cik is None or accession is None:
        return None
    rows = [h for h in holdings if h["cik"] == cik and h["report_quarter"] == quarter]
    issuers = {r["name_of_issuer"] for r in rows}
    return len(issuers), "COUNT", [accession]


def _op_total_portfolio_value(plan, filings, holdings):
    manager = _resolve_manager(plan.get("manager_name"), filings)
    quarter = _normalize_quarter(plan.get("quarter"))
    if not manager or not quarter:
        return None
    cik = next((f["cik"] for f in filings if f["fund_name"] == manager), None)
    accession = next(
        (f["accession_number"] for f in filings if f["fund_name"] == manager and f["report_quarter"] == quarter),
        None,
    )
    if cik is None or accession is None:
        return None
    rows = [h for h in holdings if h["cik"] == cik and h["report_quarter"] == quarter]
    total = sum(r["value"] or 0 for r in rows)
    return total, "USD", [accession]


def _op_managers_by_form_type(plan, filings, holdings):
    form_filter = (plan.get("form_type_filter") or "").upper()
    quarter = _normalize_quarter(plan.get("quarter"))
    if not form_filter or not quarter:
        return None
    rows = [f for f in filings if f["report_quarter"] == quarter and form_filter in f["form_type"].upper()]
    if not rows:
        return None
    names = sorted({r["fund_name"] for r in rows})
    sources = sorted({r["accession_number"] for r in rows})
    return names, "NAME", sources


def _op_manager_holds_issuer(plan, filings, holdings):
    manager = _resolve_manager(plan.get("manager_name"), filings)
    issuer = _resolve_issuer(plan.get("issuer_query"), holdings)
    quarter = _normalize_quarter(plan.get("quarter"))
    if not manager or not issuer or not quarter:
        return None
    cik = next((f["cik"] for f in filings if f["fund_name"] == manager), None)
    if cik is None:
        return None
    rows = [
        h for h in holdings
        if h["cik"] == cik and h["report_quarter"] == quarter and h["name_of_issuer"] == issuer
    ]
    sources = sorted({r["accession_number"] for r in rows})
    return len(rows), "COUNT", sources


def _op_most_of_option_type(plan, filings, holdings):
    option_type = (plan.get("option_type") or "").strip().lower()
    quarter = _normalize_quarter(plan.get("quarter"))
    if not option_type or not quarter:
        return None
    counts, accessions = {}, {}
    for h in holdings:
        if h["report_quarter"] != quarter:
            continue
        if (h["put_call"] or "").strip().lower() != option_type:
            continue
        counts[h["cik"]] = counts.get(h["cik"], 0) + 1
        accessions.setdefault(h["cik"], set()).add(h["accession_number"])
    if not counts:
        return None
    best_cik = max(sorted(counts), key=lambda c: counts[c])
    cik_to_name = {f["cik"]: f["fund_name"] for f in filings}
    manager = cik_to_name.get(best_cik)
    if manager is None:
        return None
    return manager, "NAME", sorted(accessions[best_cik])


def _op_largest_position_for_manager(plan, filings, holdings):
    manager = _resolve_manager(plan.get("manager_name"), filings)
    quarter = _normalize_quarter(plan.get("quarter"))
    if not manager or not quarter:
        return None
    cik = next((f["cik"] for f in filings if f["fund_name"] == manager), None)
    if cik is None:
        return None
    rows = [h for h in holdings if h["cik"] == cik and h["report_quarter"] == quarter]
    if not rows:
        return None
    best = max(rows, key=lambda r: r["value"] or 0)
    return best["name_of_issuer"], "NAME", [best["accession_number"]]


def _op_issuer_held_both_quarters(plan, filings, holdings):
    issuer = _resolve_issuer(plan.get("issuer_query"), holdings)
    if not issuer:
        return None
    q1_rows = [h for h in holdings if h["name_of_issuer"] == issuer and h["report_quarter"] == "2026Q1"]
    q2_rows = [h for h in holdings if h["name_of_issuer"] == issuer and h["report_quarter"] == "2026Q2"]
    both = {r["cik"] for r in q1_rows} & {r["cik"] for r in q2_rows}
    if not both:
        return None
    cik_to_name = {f["cik"]: f["fund_name"] for f in filings}
    results, sources = [], []
    for cik in sorted(both):
        q1_val = sum(r["value"] or 0 for r in q1_rows if r["cik"] == cik)
        q2_val = sum(r["value"] or 0 for r in q2_rows if r["cik"] == cik)
        direction = "grew" if q2_val > q1_val else ("shrank" if q2_val < q1_val else "unchanged")
        results.append(f"{cik_to_name.get(cik, cik)}: {direction}")
        sources.extend(r["accession_number"] for r in q1_rows if r["cik"] == cik)
        sources.extend(r["accession_number"] for r in q2_rows if r["cik"] == cik)
    answer = results[0] if len(results) == 1 else results
    return answer, "NAME", sorted(set(sources))


def _op_average_portfolio_value(plan, filings, holdings):
    quarter = _normalize_quarter(plan.get("quarter"))
    if not quarter:
        return None
    quarter_filings = [f for f in filings if f["report_quarter"] == quarter]
    if not quarter_filings:
        return None
    totals, sources = [], []
    for f in quarter_filings:
        rows = [h for h in holdings if h["cik"] == f["cik"] and h["report_quarter"] == quarter]
        totals.append(sum(r["value"] or 0 for r in rows))
        sources.append(f["accession_number"])
    return sum(totals) / len(totals), "USD", sources


OPERATIONS = {
    "largest_position_by_issuer": _op_largest_position_by_issuer,
    "issuer_change_between_quarters": _op_issuer_change_between_quarters,
    "distinct_issuer_count": _op_distinct_issuer_count,
    "total_portfolio_value": _op_total_portfolio_value,
    "managers_by_form_type": _op_managers_by_form_type,
    "manager_holds_issuer": _op_manager_holds_issuer,
    "most_of_option_type": _op_most_of_option_type,
    "largest_position_for_manager": _op_largest_position_for_manager,
    "issuer_held_both_quarters": _op_issuer_held_both_quarters,
    "average_portfolio_value": _op_average_portfolio_value,
}

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": list(OPERATIONS) + ["unsupported"]},
        "manager_name": _NULLABLE_STRING,
        "issuer_query": _NULLABLE_STRING,
        "quarter": _NULLABLE_STRING,
        "quarter_from": _NULLABLE_STRING,
        "quarter_to": _NULLABLE_STRING,
        "form_type_filter": _NULLABLE_STRING,
        "option_type": _NULLABLE_STRING,
    },
    "required": [
        "operation", "manager_name", "issuer_query", "quarter",
        "quarter_from", "quarter_to", "form_type_filter", "option_type",
    ],
}

SYSTEM_PROMPT = """You are a query planner for a dataset of SEC Form 13F filings \
covering 2026 Q1 and 2026 Q2 only. You never see the data itself -- you only classify \
the question into one supported operation and extract its parameters as plain text, \
exactly as they appear in the question. Do not normalize spellings or guess. If the \
question does not match a supported operation, or clearly asks about a period outside \
2026 Q1/Q2, use operation "unsupported".

Operations:
- largest_position_by_issuer: which manager held the largest position in a given issuer, in a given quarter.
- issuer_change_between_quarters: which manager's position in a given issuer changed the most between two quarters.
- distinct_issuer_count: how many distinct issuers a given manager reported in a given quarter.
- total_portfolio_value: total reported value of a given manager's holdings in a given quarter.
- managers_by_form_type: which managers filed a given form type (e.g. 13F-NT) in a given quarter.
- manager_holds_issuer: whether a given manager reported a position in a given issuer in a given quarter.
- most_of_option_type: which manager reported the most positions of a given option type (Call or Put) in a given quarter.
- largest_position_for_manager: a given manager's largest position by value in a given quarter, and which issuer it was.
- issuer_held_both_quarters: which manager(s) held a given issuer in both 2026 Q1 and 2026 Q2, and whether it grew or shrank.
- average_portfolio_value: average total portfolio value across all managers in a given quarter.
"""


def main(question: str) -> dict[str, Any]:
    """Answer `question` against the dataset. Never raises -- failures return a null answer."""
    null_result: dict[str, Any] = {"answer": None, "unit": "NONE", "sources": []}

    try:
        filings = _load_filings()
        holdings = _load_holdings()
    except Exception as exc:
        print(f"could not load dataset: {exc}", file=sys.stderr)
        return null_result

    try:
        plan = complete_json(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            INTENT_SCHEMA,
            max_tokens=300,
        )
    except Exception as exc:
        print(f"planning failed: {exc}", file=sys.stderr)
        return null_result

    operation = plan.get("operation") if isinstance(plan, dict) else None
    handler = OPERATIONS.get(operation)
    if handler is None:
        print(f"unsupported or unrecognised operation: {operation!r}", file=sys.stderr)
        return null_result

    try:
        result = handler(plan, filings, holdings)
    except Exception as exc:
        print(f"operation {operation!r} failed: {exc}", file=sys.stderr)
        return null_result

    if result is None:
        print(f"operation {operation!r} found no matching data for: {plan}", file=sys.stderr)
        return null_result

    answer, unit, sources = result
    if unit not in VALID_UNITS:
        unit = "NONE"
    clean_sources = [s for s in sources if isinstance(s, str) and ACCESSION_RE.match(s)]
    return {"answer": answer, "unit": unit, "sources": clean_sources}


def _cli() -> int:
    if len(sys.argv) < 2:
        print('usage: python -m agents.answer "your question"', file=sys.stderr)
        return 2

    result = main(sys.argv[1])

    if not isinstance(result, dict):
        print(f"main() must return a dict, got {type(result).__name__}", file=sys.stderr)
        return 1
    missing = {"answer", "unit", "sources"} - set(result)
    if missing:
        print(f"result missing key(s): {sorted(missing)}", file=sys.stderr)
        return 1
    if result["unit"] not in VALID_UNITS:
        print(f"unit must be one of {sorted(VALID_UNITS)}, got {result['unit']!r}", file=sys.stderr)
        return 1
    if not isinstance(result["sources"], list):
        print("sources must be a list of accession numbers", file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

