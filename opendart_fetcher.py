#!/usr/bin/env python3
"""Fetch company data and the latest annual/quarterly filings from Open DART.

Open DART serves original filing documents as XML ZIP files, not as PDF files.
This script saves the original ZIP and uses an installed Chromium/Edge browser
in headless mode to print the DART viewer page to PDF.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv()


API = "https://opendart.fss.or.kr/api"
REPORT_CODES = {"annual": "11011", "half": "11012", "q1": "11013", "q3": "11014"}
LOGGER = logging.getLogger("opendart_fetcher")
OPENDART_API_KEY = os.getenv("OPENDART_API_KEY")

class OpenDartError(RuntimeError):
    pass


def get_json(endpoint: str, key: str, **params: str) -> dict[str, Any]:
    query = {"crtfc_key": key, **{k: v for k, v in params.items() if v is not None}}
    safe_params = {k: ("***masked***" if k == "crtfc_key" else v) for k, v in query.items()}
    url = f"{API}/{endpoint}.json?{urlencode(query)}"
    LOGGER.debug("GET JSON endpoint=%s params=%s", endpoint, safe_params)
    try:
        req = Request(url, headers={"User-Agent": "CorporationSurvey/1.0"})
        with urlopen(req, timeout=60) as response:
            raw = response.read()
            LOGGER.debug("Response endpoint=%s http_status=%s bytes=%s", endpoint, response.status, len(raw))
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        LOGGER.exception("Request or JSON decoding failed endpoint=%s", endpoint)
        raise
    if payload.get("status") != "000":
        LOGGER.error("Open DART error endpoint=%s status=%s message=%s", endpoint, payload.get("status"), payload.get("message"))
        raise OpenDartError(f"{endpoint}: {payload.get('status')} {payload.get('message')}")
    LOGGER.debug("Successful endpoint=%s list_count=%s", endpoint, len(payload.get("list", [])))
    return payload


def get_bytes(endpoint: str, key: str, **params: str) -> bytes:
    query = {"crtfc_key": key, **{k: v for k, v in params.items() if v is not None}}
    safe_params = {k: ("***masked***" if k == "crtfc_key" else v) for k, v in query.items()}
    LOGGER.debug("GET binary endpoint=%s params=%s", endpoint, safe_params)
    try:
        req = Request(f"{API}/{endpoint}?{urlencode(query)}", headers={"User-Agent": "CorporationSurvey/1.0"})
        with urlopen(req, timeout=120) as response:
            content = response.read()
            LOGGER.debug("Response endpoint=%s http_status=%s bytes=%s", endpoint, response.status, len(content))
    except Exception:
        LOGGER.exception("Binary request failed endpoint=%s", endpoint)
        raise
    # document.xml returns a ZIP on success and an XML error body otherwise.
    if not content.startswith(b"PK"):
        try:
            payload = json.loads(content.decode("utf-8-sig"))
            raise OpenDartError(f"{endpoint}: {payload.get('status')} {payload.get('message')}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenDartError(f"{endpoint}: response was not a ZIP file")
    return content


def export_corporation_codes(key: str, output_path: Path) -> int:
    """Download Open DART's corporation-code ZIP and write a pipe-delimited list."""
    LOGGER.info("Downloading Open DART corporation-code registry")
    archive = get_bytes("corpCode.xml", key)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        xml_name = next((name for name in zf.namelist() if name.upper().endswith("CORPCODE.XML")), None)
        if not xml_name:
            raise OpenDartError("corpCode.xml ZIP did not contain CORPCODE.xml")
        root = ET.fromstring(zf.read(xml_name))
    records = []
    for item in root.findall(".//list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        corp_name = (item.findtext("corp_name") or "").strip()
        if corp_code and corp_name:
            records.append((corp_code, corp_name))
    records.sort(key=lambda row: row[0])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(f"{code} | {name}" for code, name in records) + "\n", encoding="utf-8-sig")
    LOGGER.info("Wrote %d corporations to %s", len(records), output_path)
    return len(records)


def clean_number(value: Any) -> int | float | None:
    if value is None:
        return None


def filing_year(report: dict[str, Any]) -> str:
    """Get fiscal year from an API row such as '사업보고서 (2025.12)'."""
    for key in ("bsns_year", "report_year", "_report_year"):
        if report.get(key):
            return str(report[key])
    match = re.search(r"\b(20\d{2})[./-]", str(report.get("report_nm", "")))
    if match:
        return match.group(1)
    raise OpenDartError(f"Could not determine fiscal year from disclosure row: {report.get('report_nm')}")


def receipt_number(report: dict[str, Any]) -> str:
    value = report.get("rcept_no") or report.get("rcp_no")
    if not value:
        raise OpenDartError(f"Could not determine receipt number from disclosure row: {report.get('report_nm')}")
    return str(value)
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text or text in {"-", "—", "–"}:
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def report_kind(name: str) -> str | None:
    if "사업보고서" in name or "Annual" in name:
        return "annual"
    if "분기보고서" in name and "반기" not in name:
        month = re.search(r"\b20\d{2}\.(03|09)\b", name)
        if month:
            return "q3" if month.group(1) == "09" else "q1"
    if "1분기" in name or "First Quarter" in name:
        return "q1"
    if "3분기" in name or "Third Quarter" in name:
        return "q3"
    return None


def list_reports(key: str, corp_code: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    end = date.today()
    # Search several years so a delayed filing or an API indexing difference
    # cannot hide the latest annual report.
    start = end - timedelta(days=1825)
    payload = get_json(
        "list", key, corp_code=corp_code,
        bgn_de=start.strftime("%Y%m%d"), end_de=end.strftime("%Y%m%d"),
        # Match the DART web screen's '정기공시' category. Search all
        # versions so a correction/final-report indexing difference does not
        # hide the original filing; choose the newest receipt later.
        pblntf_ty="A", last_reprt_at="N",
        sort="date", sort_mth="desc", page_no="1", page_count="100",
    )
    raw_rows = payload.get("list", [])
    rows = []
    for row in raw_rows:
        detail_type = str(row.get("pblntf_detail_ty", ""))
        name = str(row.get("report_nm", ""))
        kind = {"A001": "annual"}.get(detail_type) or report_kind(name)
        if detail_type == "A003":
            # Newer API/UI responses may say only '분기보고서 (YYYY.MM)'.
            month = re.search(r"\b20\d{2}\.(03|09)\b", name)
            kind = "q3" if (month and month.group(1) == "09") else "q1"
        elif "분기보고서" in name and "반기" not in name:
            month = re.search(r"\b20\d{2}\.(03|09)\b", name)
            if month:
                kind = "q3" if month.group(1) == "09" else "q1"
        if kind == "q1" and ("3분기" in name or "Third Quarter" in name):
            kind = "q3"
        # The API/UI sometimes uses the generic label '분기보고서 (YYYY.MM)'.
        # The detailed type is authoritative when it is available.
        if kind:
            row = dict(row)
            row["kind"] = kind
            rows.append(row)
    LOGGER.info("Disclosure API raw_rows=%d total_count=%s total_page=%s", len(raw_rows), payload.get("total_count"), payload.get("total_page"))
    if raw_rows:
        LOGGER.debug("Raw disclosure names=%s", [r.get("report_nm") for r in raw_rows])
    LOGGER.info("Disclosure search returned %d annual/Q1/Q3 candidates", len(rows))
    LOGGER.debug("Candidates=%s", [(r.get("kind"), r.get("report_nm"), filing_year(r), r.get("rcept_dt"), receipt_number(r)) for r in rows])
    return rows, raw_rows


def choose_reports(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    chosen: dict[str, dict[str, Any]] = {}
    for row in rows:
        kind = row["kind"]
        if kind not in chosen or str(row.get("rcept_dt", "")) > str(chosen[kind].get("rcept_dt", "")):
            chosen[kind] = row
    LOGGER.info("Selected reports=%s", {k: (v.get("report_nm"), filing_year(v), v.get("rcept_no")) for k, v in chosen.items()})
    return chosen


def financial_data(key: str, corp_code: str, report: dict[str, Any]) -> dict[str, Any]:
    year = filing_year(report)
    code = REPORT_CODES[report["kind"]]
    payload = get_json("fnlttSinglAcntAll", key, corp_code=corp_code, bsns_year=year, reprt_code=code, fs_div="CFS")
    rows = payload.get("list", [])
    wanted = ("매출", "영업이익", "자산총계", "부채총계", "자본총계", "당기순이익", "Revenue", "Operating income", "Total assets")
    selected = [r for r in rows if any(token.lower() in str(r.get("account_nm", "")).lower() for token in wanted)]
    LOGGER.info("Financial data kind=%s year=%s rows=%d key_metrics=%d", report["kind"], year, len(rows), len(selected))
    return {"report": report, "year": year, "report_code": code, "key_metrics": selected, "raw_rows": rows}


def employment_data(key: str, corp_code: str, report: dict[str, Any]) -> dict[str, Any]:
    year = filing_year(report)
    code = REPORT_CODES[report["kind"]]
    payload = get_json("empSttus", key, corp_code=corp_code, bsns_year=year, reprt_code=code)
    LOGGER.info("Employment data kind=%s year=%s rows=%d", report["kind"], year, len(payload.get("list", [])))
    return {"report": report, "year": year, "report_code": code, "rows": payload.get("list", [])}


def browser_path() -> str | None:
    candidates = [
        os.environ.get("PROGRAMFILES(X86)", "") + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("PROGRAMFILES", "") + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
        shutil.which("msedge"), shutil.which("chrome"), shutil.which("chromium"),
    ]
    return next((p for p in candidates if p and Path(p).exists()), None)


def save_pdf(report: dict[str, Any], output: Path, browser: str | None) -> dict[str, Any]:
    rcp_no = receipt_number(report)
    viewer_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
    pdf = output / f"{report['kind']}_{filing_year(report)}.pdf"
    if not browser:
        LOGGER.warning("No Edge/Chrome/Chromium browser found for kind=%s; writing viewer URL", report["kind"])
        (output / f"{report['kind']}_viewer.url.txt").write_text(viewer_url, encoding="utf-8")
        return {"status": "browser_not_found", "viewer_url": viewer_url}
    with tempfile.TemporaryDirectory(prefix="opendart-") as profile:
        command = [
            browser, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            f"--user-data-dir={profile}", f"--print-to-pdf={pdf}", viewer_url,
        ]
        LOGGER.info("Starting headless browser PDF print kind=%s url=%s", report["kind"], viewer_url)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            LOGGER.error("Headless browser timed out after 60 seconds kind=%s", report["kind"])
            (output / f"{report['kind']}_viewer.url.txt").write_text(viewer_url, encoding="utf-8")
            return {"status": "pdf_print_timeout", "viewer_url": viewer_url}
    LOGGER.debug("PDF browser returncode=%s stdout=%s stderr=%s", result.returncode, result.stdout[-500:], result.stderr[-500:])
    if result.returncode != 0 or not pdf.exists() or pdf.stat().st_size < 1000:
        LOGGER.error("PDF print failed kind=%s expected_path=%s", report["kind"], pdf)
        (output / f"{report['kind']}_viewer.url.txt").write_text(viewer_url, encoding="utf-8")
        return {"status": "pdf_print_failed", "viewer_url": viewer_url, "stderr": result.stderr[-500:]}
    return {"status": "downloaded", "path": str(pdf), "viewer_url": viewer_url}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corp-code", required=False, help="8-digit Open DART corporation code")
    parser.add_argument("--api-key", default=os.getenv("OPENDART_API_KEY"), help="Open DART key, or set OPENDART_API_KEY")
    parser.add_argument("--out-dir", default="temp", help="Output directory")
    parser.add_argument("--output-json", default=None, help="JSON output path; defaults to <out-dir>/output.json")
    parser.add_argument("--debug", action="store_true", help="Enable detailed console and file logging")
    parser.add_argument("--log-file", default=None, help="Debug log path; defaults to <out-dir>/opendart_debug.log")
    parser.add_argument("--test-connection", action="store_true", help="Test API key and corporation code, then exit")
    parser.add_argument("--skip-pdf", action="store_true", help="Skip slow headless-browser PDF generation")
    parser.add_argument("--export-corp-list", metavar="PATH", help="Export all corp-code | company name rows to PATH")
    args = parser.parse_args()
    if not args.api_key:
        parser.error("--api-key or OPENDART_API_KEY is required")
    if args.export_corp_list:
        export_path = Path(args.export_corp_list).resolve()
        count = export_corporation_codes(args.api_key, export_path)
        print(json.dumps({"ok": True, "count": count, "path": str(export_path)}, ensure_ascii=False, indent=2))
        return 0
    if not args.corp_code or not re.fullmatch(r"\d{8}", args.corp_code):
        parser.error("--corp-code must be exactly 8 digits")

    output = Path(args.out_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    output_json = Path(args.output_json).resolve() if args.output_json else output / "output.json"
    log_file = Path(args.log_file).resolve() if args.log_file else output / "opendart_debug.log"
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )
    LOGGER.info("Starting Open DART fetch corp_code=%s output_dir=%s output_json=%s", args.corp_code, output, output_json)
    if args.test_connection:
        LOGGER.info("Connection test: calling company overview endpoint")
        try:
            company = get_json("company", args.api_key, corp_code=args.corp_code)
            print(json.dumps({
                "ok": True,
                "message": "Open DART API key and corporation code are valid",
                "corp_code": args.corp_code,
                "corp_name": company.get("corp_name"),
                "stock_code": company.get("stock_code"),
                "status": company.get("status"),
            }, ensure_ascii=False, indent=2))
            return 0
        except OpenDartError as exc:
            LOGGER.error("Connection test failed: %s", exc)
            print(json.dumps({
                "ok": False,
                "message": str(exc),
                "corp_code": args.corp_code,
            }, ensure_ascii=False, indent=2))
            return 2

    LOGGER.info("Step 1/6: searching disclosures")
    rows, raw_disclosures = list_reports(args.api_key, args.corp_code)
    reports = choose_reports(rows)
    if "annual" not in reports:
        # Keep a useful artifact even when the API returns no recognizable
        # annual report, instead of failing before output.json is written.
        diagnostic = {
            "corp_code": args.corp_code,
            "error": "No annual report was found in the Open DART disclosure search results",
            "disclosure_candidates": raw_disclosures,
        }
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.error("No annual report recognized; diagnostic JSON written to %s", output_json)
        raise OpenDartError(f"No annual report was found; diagnostic JSON written to {output_json}")
    LOGGER.info("Step 2/6: fetching company overview")
    selected = {k: reports[k] for k in ("annual", "q1", "q3") if k in reports}
    try:
        company = get_json("company", args.api_key, corp_code=args.corp_code)
        result: dict[str, Any] = {"corp_code": args.corp_code, "company": company, "reports": selected, "financials": {}, "employment": {}, "pdf": {}}
    except OpenDartError as exc:
        result = {"corp_code": args.corp_code, "company_error": str(exc), "reports": selected, "financials": {}, "employment": {}, "pdf": {}}
    for kind, report in selected.items():
        LOGGER.info("Step 3/6: fetching financial data kind=%s", kind)
        try:
            result["financials"][kind] = financial_data(args.api_key, args.corp_code, report)
        except OpenDartError as exc:
            result["financials"][kind] = {"error": str(exc), "report": report}
        try:
            LOGGER.info("Step 4/6: fetching employment data kind=%s", kind)
            result["employment"][kind] = employment_data(args.api_key, args.corp_code, report)
        except OpenDartError as exc:
            result["employment"][kind] = {"error": str(exc), "report": report}
        zip_path = output / f"{kind}_{filing_year(report)}_source.zip"
        try:
            LOGGER.info("Step 5/6: downloading original document kind=%s to %s", kind, zip_path)
            zip_path.write_bytes(get_bytes("document.xml", args.api_key, rcept_no=receipt_number(report)))
            LOGGER.info("Saved original document bytes=%s", zip_path.stat().st_size)
        except OpenDartError as exc:
            result.setdefault("document_errors", {})[kind] = str(exc)
            LOGGER.error("Original document download failed kind=%s: %s", kind, exc)
        if args.skip_pdf:
            result["pdf"][kind] = {
                "status": "skipped",
                "reason": "--skip-pdf was specified",
                "viewer_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_number(report)}",
            }
            LOGGER.info("Step 6/6: skipping PDF generation kind=%s", kind)
        else:
            LOGGER.info("Step 6/6: creating PDF kind=%s", kind)
            result["pdf"][kind] = save_pdf(report, output, browser_path())
            LOGGER.info("PDF result kind=%s status=%s", kind, result["pdf"][kind].get("status"))
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Step 6/6: completed JSON write path=%s bytes=%s", output_json, output_json.stat().st_size)
    print(json.dumps({"output": str(output), "reports": list(selected), "json": str(output_json)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpenDartError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
