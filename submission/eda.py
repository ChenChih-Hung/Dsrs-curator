
"""Chapter 2 - Interrogate.

Explores the filings downloaded in Chapter 1 (output/filings/) to map each
SCHEMA.md column to its source in the raw XML, and to surface real
inconsistencies across filers that Chapter 3's parser needs to handle.

Run:
    python submission/eda.py
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
FILINGS_DIR = ROOT / "output" / "filings"

COMMON_NS = "http://www.sec.gov/edgar/common"


def local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def child(el, name):
    if el is None:
        return None
    for c in el:
        if not isinstance(c.tag, str):
            continue
        if local(c.tag) == name:
            return c
    return None


def text_at(el, path):
    node = el
    for step in path.split("/"):
        node = child(node, step)
        if node is None:
            return None
    return node.text


def cover_paths():
    return sorted(FILINGS_DIR.glob("*/*.cover.xml"))


def holdings_paths():
    return [p for p in sorted(FILINGS_DIR.glob("*/*.xml")) if not p.name.endswith(".cover.xml")]


def main():
    covers = cover_paths()
    holdings = holdings_paths()
    print(f"{len(covers)} cover documents, {len(holdings)} main documents.\n")

    print("--- namespace prefixes bound to the common-elements namespace ---")
    prefixes = Counter()
    for p in covers:
        root = etree.parse(str(p)).getroot()
        for prefix, uri in (root.nsmap or {}).items():
            if uri == COMMON_NS:
                prefixes[prefix] += 1
    print(dict(prefixes))
    print()

    print("--- tableEntryTotal (declared) vs actual informationTable rows ---")
    checked, mismatches = 0, []
    for cover_path in covers:
        accession = cover_path.name.replace(".cover.xml", "")
        main_path = cover_path.parent / f"{accession}.xml"
        if not main_path.exists():
            continue
        main_root = etree.parse(str(main_path)).getroot()
        if local(main_root.tag) != "informationTable":
            continue
        checked += 1
        actual = len(main_root)
        cover_root = etree.parse(str(cover_path)).getroot()
        declared = text_at(cover_root, "formData/coverPage/summaryPage/tableEntryTotal")
        declared = int(declared) if declared else None
        if declared is not None and declared != actual:
            mismatches.append((accession, declared, actual))
    print(f"checked {checked} holdings filings")
    print(f"mismatches: {mismatches if mismatches else 'none'}")
    print()

    print("--- holdings-row quirks (sample across filers) ---")
    put_call_values = Counter()
    letter_cusips = []
    missing_figi = 0
    other_manager_examples = []
    n_rows = 0
    for p in holdings[:10]:
        root = etree.parse(str(p)).getroot()
        if local(root.tag) != "informationTable":
            continue
        for entry in root:
            n_rows += 1
            cusip = text_at(entry, "cusip")
            if cusip and not cusip[0].isdigit():
                letter_cusips.append((p.name, cusip))
            pc = text_at(entry, "putCall")
            if pc:
                put_call_values[pc] += 1
            if not text_at(entry, "figi"):
                missing_figi += 1
            om = text_at(entry, "otherManager")
            if om and len(other_manager_examples) < 3:
                other_manager_examples.append((p.name, om))
    print(f"sampled {n_rows} rows across {min(10, len(holdings))} filings")
    print(f"put_call casings seen: {dict(put_call_values)}")
    print(f"letter-leading CUSIPs (CINS candidates): {len(letter_cusips)} -> {letter_cusips[:5]}")
    print(f"rows missing figi: {missing_figi}/{n_rows}")
    print(f"otherManager reference examples: {other_manager_examples}")
    print()

    print(FINDINGS)


FINDINGS = """\
FINDINGS

1. Namespace prefixes for the shared common-elements namespace differ
   across filers -- e.g. Citadel's cover page (0001104659-26-097200)
   binds it to 'ns1', Renaissance Technologies' (0001037389-26-000059)
   binds the identical URI to 'com'. A parser matching on literal
   prefixed tag strings would miss the field in one of the two. Match
   on (namespace URI, local name), never on the raw tag string.

2. The list of other managers whose holdings are folded into a filing
   lives under formData/coverPage/summaryPage/otherManagers2Info/
   otherManager2/otherManager -- not the more guessable
   'otherManagersInfo/otherManager'. Only found by opening a real file.

3. Declared tableEntryTotal vs actual informationTable row counts --
   see numbers printed above.

4. Optional fields (figi, putCall, otherManager, isConfidentialOmitted)
   are absent, not merely empty, when not applicable, and different
   filers include different optional cover-page fields.

5. put_call casing is inconsistent across filers where present -- see
   values printed above; must be normalized, not compared case-sensitively.

6. voting_none is sourced from an XML element literally named 'None' --
   a parser must treat it as an ordinary local-name lookup.
"""


if __name__ == "__main__":
    main()