#!/usr/bin/env python3
"""Run the whole pipeline: fetch from EDGAR, parse, write the dataset.

    python main.py --user-agent "FirstName LastName netid@illinois.edu"

This is how we run your submission, so it must work from a clean checkout with nothing
in output/. Everything below is yours to rewrite — add modules, packages, classes,
whatever fits. Only two things are fixed:

  - this file is the entry point, and it accepts --user-agent
  - it writes output/filings.parquet and output/holdings.parquet

Start with docs/01-source.md. Check your output with `python verify.py`.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import time
from collections import defaultdict

import httpx
from lxml import etree
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
FILERS = ROOT / "filers.csv"
OUTPUT = ROOT / "output"

# Scope. See docs/01-source.md — filter report periods on reportDate, and exclude
# anything accepted after the cutoff.
REPORT_PERIODS = ("2026-03-31", "2026-06-30")
FILING_DATE_CUTOFF = "2026-08-18"  # inclusive
IN_SCOPE_FORMS = {"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"}

# SEC rejects requests without a contact address, and a run that gets the department
# blocked is worth failing fast on. Loose on purpose: we check an address is present,
# not that it is well-formed.
UA_PATTERN = re.compile(r"^\S.*\s+[^@\s]+@[^@\s]+\.[a-z]{2,}\s*$", re.I)


def load_filers() -> list[dict[str, str]]:
    """The roster the researcher supplied. At least one CIK in here is wrong."""
    with FILERS.open(newline="") as fh:
        return list(csv.DictReader(fh))
def normalize(name):
    s = name.upper().strip()
    if s.startswith("THE "):
        s = s[4:]
    if len(s) > 3 and s[-3] == "/":
        s = s[:-3]
    if "(" in s and ")" in s:
        start = s.index("(")
        end = s.index(")")
        s = s[:start] + s[end + 1:]
    s = s.replace(",", "").replace(".", "")
    s = " ".join(s.split())
    return s


def verify_ciks(roster, headers):
    """Check the given CIKs against SEC's lookup file, correct the wrong ones."""
    cache_path = ROOT / ".cache" / "cik-lookup-data.txt"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists():
        text = cache_path.read_text(errors="replace")
    else:
        resp = httpx.get("https://www.sec.gov/Archives/edgar/cik-lookup-data.txt", headers=headers)
        cache_path.write_bytes(resp.content)
        text = resp.text

    cik_to_norm_names = defaultdict(set)
    norm_name_to_cik = defaultdict(set)
    for line in text.splitlines():
        if ":" not in line:
            continue
        parts = line.split(":")
        name, cik = parts[0], parts[1]
        if not cik:
            continue
        cik_padded = cik.zfill(10)
        norm = normalize(name)
        cik_to_norm_names[cik_padded].add(norm)
        norm_name_to_cik[norm].add(cik_padded)

    verified = []
    for row in roster:
        fund_name = row["fund_name"]
        given_cik = row["cik"].zfill(10)
        key = normalize(fund_name)

        if key in cik_to_norm_names.get(given_cik, set()):
            verified.append({"fund_name": fund_name, "cik": given_cik, "cik_source": "given"})
        else:
            candidates = norm_name_to_cik.get(key)
            if candidates:
                corrected_cik = sorted(candidates)[0]
                verified.append({"fund_name": fund_name, "cik": corrected_cik, "cik_source": "corrected"})
            else:
                print("WARNING: could not verify", fund_name)

    # Manual override: "Tudor Investment Corp" normalizes to an exact string match
    # against a dormant registrant (CIK 0001080384, last filed 2007). The entity that
    # actually files 13F is registered as "TUDOR INVESTMENT CORP ET AL" (0000923093).
    # See submission/ASSUMPTIONS.md.
    for v in verified:
        if v["fund_name"] == "Tudor Investment Corp":
            v["cik"] = "0000923093"

    return sorted(verified, key=lambda v: int(v["cik"]))


def write_filers_csv(verified_sorted, output):
    with open(output / "filers.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["fund_name", "cik", "cik_source"])
        for v in verified_sorted:
            writer.writerow([v["fund_name"], str(int(v["cik"])), v["cik_source"]])


def find_filings(cik, headers):
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = httpx.get(submissions_url, headers=headers)
    data = resp.json()
    recent = data["filings"]["recent"]
    matches = []
    for i in range(len(recent["form"])):
        form = recent["form"][i]
        report_date = recent["reportDate"][i]
        filing_date = recent["filingDate"][i]
        if form in IN_SCOPE_FORMS and report_date in REPORT_PERIODS and filing_date <= FILING_DATE_CUTOFF:
            matches.append({
                "cik": cik,
                "accession_number": recent["accessionNumber"][i],
                "form_type": form,
                "filing_date": filing_date,
                "report_date": report_date,
            })
    return matches


def collect_all_filings(verified_sorted, headers):
    all_filings = []
    for v in verified_sorted:
        padded_cik = v["cik"].zfill(10)
        matches = find_filings(padded_cik, headers)
        all_filings.extend(matches)
        time.sleep(0.2)
    return all_filings


def local_tag(tag):
    if "}" in tag:
        return tag.split("}")[-1]
    return tag


def fetch_filing_document(cik_nopad, accession_nodash, headers):
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_nopad}/{accession_nodash}/index.json"
    resp = httpx.get(index_url, headers=headers)
    time.sleep(0.15)
    items = resp.json()["directory"]["item"]
    xml_names = [item["name"] for item in items if item["name"].endswith(".xml")]

    info_table_bytes = None
    primary_bytes = None

    for name in xml_names:
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_nopad}/{accession_nodash}/{name}"
        doc_resp = httpx.get(doc_url, headers=headers)
        time.sleep(0.15)
        root = etree.fromstring(doc_resp.content)
        tag = local_tag(root.tag)
        if tag == "informationTable":
            info_table_bytes = doc_resp.content
        elif tag == "edgarSubmission":
            primary_bytes = doc_resp.content
    return info_table_bytes, primary_bytes       

def save_filing(filing, headers, output):
    cik_nopad = str(int(filing["cik"]))
    accession_nodash = filing["accession_number"].replace("-", "")

    out_dir = output / "filings" / cik_nopad
    out_dir.mkdir(parents=True, exist_ok=True)
    main_path = out_dir / f"{accession_nodash}.xml"
    cover_path = out_dir / f"{accession_nodash}.cover.xml"

    if main_path.exists() and cover_path.exists():
        print("cached:", main_path)
        return

    info_bytes, primary_bytes = fetch_filing_document(cik_nopad, accession_nodash, headers)
    main_bytes = info_bytes if info_bytes is not None else primary_bytes

    main_path.write_bytes(main_bytes)
    if primary_bytes is not None:
        cover_path.write_bytes(primary_bytes)
    print("downloaded:", main_path)

FILINGS_SCHEMA = pa.schema([
    ("accession_number", pa.string()),
    ("cik", pa.string()),
    ("fund_name", pa.string()),
    ("filing_manager", pa.string()),
    ("form_type", pa.string()),
    ("report_period", pa.date32()),
    ("report_quarter", pa.string()),
    ("filing_date", pa.date32()),
    ("is_amendment", pa.bool_()),
    ("amendment_no", pa.int32()),
    ("amendment_type", pa.string()),
    ("report_type", pa.string()),
    ("form_13f_file_number", pa.string()),
    ("crd_number", pa.string()),
    ("sec_file_number", pa.string()),
    ("other_included_managers_count", pa.int32()),
    ("table_entry_total", pa.int64()),
    ("table_value_total", pa.int64()),
])

HOLDINGS_SCHEMA = pa.schema([
    ("accession_number", pa.string()),
    ("cik", pa.string()),
    ("report_quarter", pa.string()),
    ("name_of_issuer", pa.string()),
    ("title_of_class", pa.string()),
    ("cusip", pa.string()),
    ("figi", pa.string()),
    ("value", pa.int64()),
    ("ssh_prnamt", pa.int64()),
    ("ssh_prnamt_type", pa.string()),
    ("put_call", pa.string()),
    ("investment_discretion", pa.string()),
    ("other_manager", pa.string()),
    ("voting_sole", pa.int64()),
    ("voting_shared", pa.int64()),
    ("voting_none", pa.int64()),
])


def xml_child(el, name):
    if el is None:
        return None
    for c in el:
        if not isinstance(c.tag, str):
            continue
        if local_tag(c.tag) == name:
            return c
    return None


def xml_text(el, path):
    node = el
    for step in path.split("/"):
        node = xml_child(node, step)
        if node is None:
            return None
    return node.text


def parse_mmddyyyy(s):
    if not s:
        return None
    mm, dd, yyyy = s.split("-")
    return date(int(yyyy), int(mm), int(dd))


def report_quarter_of(d):
    q = (d.month - 1) // 3 + 1
    return f"{d.year}Q{q}"


def build_filing_row(filing, cover_root, fund_name):
    header = xml_child(cover_root, "headerData")
    filer_info = xml_child(header, "filerInfo")
    report_period = parse_mmddyyyy(xml_text(filer_info, "periodOfReport"))

    form_data = xml_child(cover_root, "formData")
    cover_page = xml_child(form_data, "coverPage")
    summary_page = xml_child(cover_page, "summaryPage")

    form_type = filing["form_type"]
    is_amendment = form_type.endswith("/A")
    amendment_no_text = xml_text(cover_page, "amendmentNo") if is_amendment else None
    amendment_no = int(amendment_no_text) if amendment_no_text else None
    amendment_type = xml_text(cover_page, "amendmentInfo/amendmentType") if is_amendment else None

    other_count_text = xml_text(summary_page, "otherIncludedManagersCount")
    entry_total_text = xml_text(summary_page, "tableEntryTotal")
    value_total_text = xml_text(summary_page, "tableValueTotal")

    return {
        "accession_number": filing["accession_number"],
        "cik": filing["cik"],
        "fund_name": fund_name,
        "filing_manager": xml_text(cover_page, "filingManager/name"),
        "form_type": form_type,
        "report_period": report_period,
        "report_quarter": report_quarter_of(report_period),
        "filing_date": date.fromisoformat(filing["filing_date"]),
        "is_amendment": is_amendment,
        "amendment_no": amendment_no,
        "amendment_type": amendment_type,
        "report_type": xml_text(cover_page, "reportType"),
        "form_13f_file_number": xml_text(cover_page, "form13FFileNumber"),
        "crd_number": xml_text(cover_page, "crdNumber"),
        "sec_file_number": xml_text(cover_page, "secFileNumber"),
        "other_included_managers_count": int(other_count_text) if other_count_text is not None else None,
        "table_entry_total": int(entry_total_text) if entry_total_text else None,
        "table_value_total": int(value_total_text) if value_total_text else None,
    }


def build_holding_rows(main_root, accession_number, cik, report_quarter):
    rows = []
    for entry in main_root:
        if not isinstance(entry.tag, str):
            continue
        shrs = xml_child(entry, "shrsOrPrnAmt")
        voting = xml_child(entry, "votingAuthority")

        value_text = xml_text(entry, "value")
        ssh_text = xml_text(shrs, "sshPrnamt")

        rows.append({
            "accession_number": accession_number,
            "cik": cik,
            "report_quarter": report_quarter,
            "name_of_issuer": xml_text(entry, "nameOfIssuer"),
            "title_of_class": xml_text(entry, "titleOfClass"),
            "cusip": xml_text(entry, "cusip"),
            "figi": xml_text(entry, "figi"),
            "value": int(value_text) if value_text else None,
            "ssh_prnamt": int(ssh_text) if ssh_text else None,
            "ssh_prnamt_type": xml_text(shrs, "sshPrnamtType"),
            "put_call": xml_text(entry, "putCall"),
            "investment_discretion": xml_text(entry, "investmentDiscretion"),
            "other_manager": xml_text(entry, "otherManager"),
            "voting_sole": int(xml_text(voting, "Sole") or 0),
            "voting_shared": int(xml_text(voting, "Shared") or 0),
            "voting_none": int(xml_text(voting, "None") or 0),
        })
    return rows


def build_parquet_files(verified_sorted, all_filings, output):
    cik_to_fund_name = {v["cik"]: v["fund_name"] for v in verified_sorted}

    filings_rows = []
    holdings_rows = []

    for filing in all_filings:
        cik_nopad = str(int(filing["cik"]))
        accession_nodash = filing["accession_number"].replace("-", "")
        filing_dir = output / "filings" / cik_nopad
        cover_path = filing_dir / f"{accession_nodash}.cover.xml"
        main_path = filing_dir / f"{accession_nodash}.xml"

        cover_root = etree.parse(str(cover_path)).getroot()
        fund_name = cik_to_fund_name[filing["cik"]]
        row = build_filing_row(filing, cover_root, fund_name)
        filings_rows.append(row)

        main_root = etree.parse(str(main_path)).getroot()
        if local_tag(main_root.tag) == "informationTable":
            holdings_rows.extend(
                build_holding_rows(main_root, filing["accession_number"], filing["cik"], row["report_quarter"])
            )

    filings_table = pa.Table.from_pylist(filings_rows, schema=FILINGS_SCHEMA)
    pq.write_table(filings_table, output / "filings.parquet", compression="snappy")

    holdings_table = pa.Table.from_pylist(holdings_rows, schema=HOLDINGS_SCHEMA)
    pq.write_table(holdings_table, output / "holdings.parquet", compression="snappy")

    print(f"wrote {len(filings_rows)} filing rows, {len(holdings_rows)} holding rows")


def run(user_agent: str, output: Path) -> None:
    """Build the dataset.

    Suggested shape, not a requirement:

        1. verify the CIKs against SEC's lookup file      docs/01-source.md
        2. find in-scope filings via the submissions API
        3. download the filings, caching as you go
        4. parse into the schema                          docs/SCHEMA.md
        5. write output/filings.parquet and output/holdings.parquet
    """
    headers = {"User-Agent": user_agent}

    roster = load_filers()
    verified_sorted = verify_ciks(roster, headers)
    write_filers_csv(verified_sorted, output)

    all_filings = collect_all_filings(verified_sorted, headers)
    for filing in all_filings:
        save_filing(filing, headers, output)
    build_parquet_files(verified_sorted, all_filings, output)
    print(f"done: {len(verified_sorted)} filers, {len(all_filings)} filings")



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user-agent", required=True,
                    help='required by SEC: "FirstName LastName netid@illinois.edu"')
    ap.add_argument("--output", type=Path, default=OUTPUT)
    args = ap.parse_args()

    if not UA_PATTERN.match(args.user_agent):
        sys.exit(
            "invalid --user-agent.\n"
            "SEC requires a contact address and rejects requests without one.\n"
            '  python main.py --user-agent "Jane Doe jdoe@illinois.edu"'
        )

    args.output.mkdir(parents=True, exist_ok=True)
    run(args.user_agent, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
