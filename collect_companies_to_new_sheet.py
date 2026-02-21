import argparse
import re
from typing import Optional, List, Dict, Set, Tuple

import gspread


SOURCE_GIDS_DEFAULT = [2061399293, 41801433, 343674200]

COMPANY_HEADER_CANDIDATES = ["Company", "COMPANY", "Company Name", "Name"]
EMAIL_HEADER_CANDIDATES = ["Email", "EMAIL", "Emails", "E-mail", "E-Mail"]
PHONE_HEADER_CANDIDATES = ["Phone", "PHONE", "Phones", "Telephone", "Tel"]


def normalize_header(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\s_\-]+", " ", s)
    return s


def find_header_index(headers: List[str], candidates: List[str]) -> Optional[int]:
    norm_headers = [normalize_header(h) for h in headers]
    cand = {normalize_header(c) for c in candidates}
    for i, h in enumerate(norm_headers):
        if h in cand:
            return i
    return None


def find_website_index_strict(headers: List[str]) -> Optional[int]:
    # Only accept a column whose header is exactly "website" (any case).
    for i, h in enumerate(headers):
        if (h or "").strip().lower() == "website":
            return i
    return None


def open_worksheet_by_gid(sh, gid: int):
    try:
        ws = sh.get_worksheet_by_id(gid)
        if ws is not None:
            return ws
    except Exception:
        pass

    for ws in sh.worksheets():
        if getattr(ws, "id", None) == gid:
            return ws

    raise RuntimeError(f"Worksheet gid {gid} not found in this spreadsheet.")


def cell(row: List[str], idx: Optional[int]) -> str:
    if idx is None:
        return ""
    if idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def ensure_dest_ws(sh, title: str):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=1000, cols=4)


def merge_value_set(store: Set[str], value: str):
    v = (value or "").strip()
    if not v:
        return
    store.add(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-id", required=True, help="Google Sheet ID")
    ap.add_argument("--service-account", required=True, help="Path to service account JSON")
    ap.add_argument("--dest-worksheet", default="Company List", help="Destination tab name")
    ap.add_argument("--source-gid", type=int, action="append", default=[], help="Source tab gid (repeatable)")
    ap.add_argument("--reset-dest", action="store_true", help="Clear destination and rewrite")
    args = ap.parse_args()

    source_gids = args.source_gid if args.source_gid else SOURCE_GIDS_DEFAULT

    gc = gspread.service_account(filename=args.service_account)
    sh = gc.open_by_key(args.sheet_id)

    # company_key -> {"company": str, "websites": set, "emails": set, "phones": set}
    agg: Dict[str, Dict[str, object]] = {}

    for gid in source_gids:
        ws = open_worksheet_by_gid(sh, gid)
        values = ws.get_all_values()
        if not values:
            continue

        headers = values[0]
        idx_company = find_header_index(headers, COMPANY_HEADER_CANDIDATES)
        idx_website = find_website_index_strict(headers)  # strict: only WEBSITE/website header
        idx_email = find_header_index(headers, EMAIL_HEADER_CANDIDATES)
        idx_phone = find_header_index(headers, PHONE_HEADER_CANDIDATES)

        # If no header match for company, assume column A is company and include row 1 as data.
        start_row = 1
        if idx_company is None:
            idx_company = 0
            start_row = 0

        for r in values[start_row:]:
            name = cell(r, idx_company)
            if not name:
                continue

            key = name.strip().lower()
            if key not in agg:
                agg[key] = {
                    "company": name.strip(),
                    "websites": set(),
                    "emails": set(),
                    "phones": set(),
                }

            merge_value_set(agg[key]["websites"], cell(r, idx_website))
            merge_value_set(agg[key]["emails"], cell(r, idx_email))
            merge_value_set(agg[key]["phones"], cell(r, idx_phone))

    # Build output rows
    out: List[List[str]] = [["Company", "Website", "Email", "Phone"]]

    for key in sorted(agg.keys()):
        entry = agg[key]
        company = entry["company"]  # type: ignore[assignment]
        websites = sorted(entry["websites"])  # type: ignore[arg-type]
        emails = sorted(entry["emails"])      # type: ignore[arg-type]
        phones = sorted(entry["phones"])      # type: ignore[arg-type]

        out.append([
            str(company),
            "; ".join(websites),
            "; ".join(emails),
            "; ".join(phones),
        ])

    dest = ensure_dest_ws(sh, args.dest_worksheet)

    if args.reset_dest:
        dest.clear()

    rows_needed = max(len(out), 2)
    dest.resize(rows=rows_needed, cols=4)
    dest.update(values=out, range_name=f"A1:D{len(out)}")


if __name__ == "__main__":
    main()
