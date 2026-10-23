#!/usr/bin/env python3
"""Local Corporation Survey dashboard server."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import zipfile
from datetime import date, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv()

API = "https://opendart.fss.or.kr/api"
ROOT = Path(__file__).resolve().parent
ROUTE_LOCK = threading.Lock()
ROUTE_WORKER = None
ROUTE_WORKER_MTIME = None


class DashboardServer(ThreadingHTTPServer):
    # Render can restart the Linux listener while prior connections are still
    # winding down. Keep exclusive binding on Windows for local runs.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class DartError(RuntimeError):
    pass


def dart_json(key: str, endpoint: str, **params):
    query = {"crtfc_key": key, **{k: v for k, v in params.items() if v is not None}}
    req = Request(f"{API}/{endpoint}.json?" + urlencode(query), headers={"User-Agent": "CorporationSurveyDashboard/1.0"})
    with urlopen(req, timeout=45) as response:
        data = json.loads(response.read().decode("utf-8-sig"))
    if data.get("status") != "000":
        raise DartError(f"{endpoint}: {data.get('status')} {data.get('message')}")
    return data


def load_corporations(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if " | " not in line:
            continue
        code, name = line.split(" | ", 1)
        if re.fullmatch(r"\d{8}", code.strip()) and name.strip():
            rows.append({"corp_code": code.strip(), "corp_name": name.strip()})
    return rows


def number(value):
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "—"}:
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


@lru_cache(maxsize=128)
def filing_employment_total(key, receipt):
    """Read the disclosed total row omitted by some empSttus API responses."""
    if not re.fullmatch(r"\d{14}", str(receipt or "")):
        return None
    query = urlencode({"crtfc_key": key, "rcept_no": receipt})
    req = Request(f"{API}/document.xml?{query}", headers={"User-Agent": "CorporationSurveyDashboard/1.0"})
    with urlopen(req, timeout=45) as response:
        archive = response.read()
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        for name in files.namelist():
            if not name.lower().endswith(".xml"):
                continue
            text = files.read(name).decode("utf-8-sig", errors="replace")
            tables = re.finditer(r'<TABLE-GROUP\b[^>]*\bACLASS="EMPLOYEE"[^>]*>(.*?)</TABLE-GROUP>', text, re.S | re.I)
            for table in tables:
                def field(code):
                    match = re.search(r'<(?:TE|TD)\b[^>]*\bACODE="' + code + r'"[^>]*>([^<]*)', table.group(1), re.I)
                    return number(match.group(1)) if match else None
                count, average_million = field("EMP_CNT"), field("EMP_VICT")
                if count is not None and average_million is not None:
                    return {"employees": count, "average_payroll": average_million * 1_000_000,
                            "total_row": {"rgllbr_co": field("EMP_REOT"),
                                          "rgllbr_abacpt_labrr_co": field("EMP_REOST"),
                                          "cnttk_co": field("EMP_COCT"),
                                          "cnttk_abacpt_labrr_co": field("EMP_COCST"),
                                          "sm": count, "avrg_cnwk_sdytrn": field("EMP_AVYT"),
                                          "fyer_salary_totamt": field("EMP_AICT"),
                                          "jan_salary_am": average_million}}
    return None


def employment_summary(rows, filing_total=None):
    """Use filing totals without counting division rows and gender subtotals twice."""
    label = lambda value: re.sub(r"\s+", "", str(value or ""))
    is_overall = lambda row: label(row.get("fo_bbm")) in {"합계", "총계", "전체"} or label(row.get("sexdstn")) in {"합계", "총계", "전체"}
    overall = next((row for row in rows if is_overall(row)), None)
    subtotals = [row for row in rows if label(row.get("fo_bbm")) == "성별합계"]
    basis = subtotals or [row for row in rows if not is_overall(row)]
    employees = filing_total.get("employees") if filing_total else None
    if employees is None:
        employees = number(overall.get("sm")) if overall else None
    if employees is None:
        counts = [number(row.get("sm")) for row in basis]
        employees = sum(value for value in counts if value is not None) if any(value is not None for value in counts) else None

    payroll = filing_total.get("average_payroll") if filing_total else None
    if payroll is None:
        payroll = number(overall.get("jan_salary_am")) if overall else None
    estimated = False
    source = "filing" if payroll is not None and filing_total else "api" if payroll is not None else "unavailable"
    if payroll is None:
        weighted = [(number(row.get("sm")), number(row.get("jan_salary_am"))) for row in basis]
        weighted = [(count, salary) for count, salary in weighted if count is not None and count > 0 and salary is not None]
        if weighted:
            payroll = round(sum(count * salary for count, salary in weighted) / sum(count for count, _ in weighted))
            estimated = True
            source = "estimated"
    return {"employees": employees, "average_payroll": payroll, "payroll_estimated": estimated,
            "payroll_source": source, "total_row": filing_total.get("total_row") if filing_total else None}


def reported_employment_summary(key, rows):
    total = None
    if rows:
        try:
            total = filing_employment_total(key, rows[0].get("rcept_no"))
        except (OSError, ValueError, zipfile.BadZipFile):
            pass
    return employment_summary(rows, total)


def metric_rows(rows, amount_field="thstrm_amount"):
    patterns = {
        "revenue": ("매출액", "영업수익", "Revenue"),
        "operating_income": ("영업이익", "영업손익", "영업손실", "Operating income", "Operating loss"),
        "net_income": ("당기순이익", "당기순손익", "분기순이익", "분기순손익", "Net income"),
        "assets": ("자산총계", "Total assets"),
        "liabilities": ("부채총계", "Total liabilities"),
        "equity": ("자본총계", "Total equity"),
    }
    account_ids = {
        "revenue": {"ifrs-full_Revenue", "ifrs_Revenue", "dart_Revenue"},
        "operating_income": {"dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"},
        "net_income": {"ifrs-full_ProfitLoss", "ifrs_ProfitLoss"},
        "assets": {"ifrs-full_Assets", "ifrs_Assets"},
        "liabilities": {"ifrs-full_Liabilities", "ifrs_Liabilities"},
        "equity": {"ifrs-full_Equity", "ifrs_Equity"},
    }
    result = {}
    for key, names in patterns.items():
        statement = {"BS"} if key in {"assets", "liabilities", "equity"} else {"IS", "CIS"}
        candidates = [r for r in rows if r.get("sj_div") in statement or not r.get("sj_div")]
        row = next((r for r in candidates if r.get("account_id") in account_ids[key]
                    and number(r.get(amount_field)) is not None), None)
        if row is None:
            row = next((r for r in candidates if any(name.lower() in str(r.get("account_nm", "")).lower() for name in names)
                        and number(r.get(amount_field)) is not None), None)
        if row:
            raw_amount = number(row.get(amount_field))
            # Open DART returns these account amounts in KRW. The dashboard
            # presents every financial figure in million KRW.
            result[key] = round(raw_amount / 1_000_000, 2) if raw_amount is not None else None
    return result


def with_margin(metrics):
    revenue = metrics.get("revenue")
    operating = metrics.get("operating_income")
    metrics = dict(metrics)
    metrics["operating_income_pct"] = round(operating / revenue * 100, 1) if operating is not None and revenue not in (None, 0) else None
    return metrics


def annual_series(key, corp_code):
    current = date.today().year
    series = []
    for year in range(current - 4, current + 1):
        try:
            data = dart_json(key, "fnlttSinglAcntAll", corp_code=corp_code, bsns_year=str(year), reprt_code="11011", fs_div="CFS")
            metrics = with_margin(metric_rows(data.get("list", [])))
            if metrics:
                series.append({"year": year, **metrics})
        except DartError:
            continue
    return series


def disclosure_kind(row):
    name = str(row.get("report_nm", ""))
    detail = str(row.get("pblntf_detail_ty", "")).strip().upper()
    if detail == "A001" or "사업보고서" in name:
        return "annual"
    if detail == "A002" or "반기보고서" in name:
        return "11012"
    if detail == "A003" or "분기보고서" in name:
        match = re.search(r"20\d{2}\.(03|09)", name)
        if match:
            return "11014" if match.group(1) == "09" else "11013"
    return None


def quarterly_series(key, corp_code):
    end = date.today()
    start = end - timedelta(days=400)
    data = dart_json(key, "list", corp_code=corp_code, bgn_de=start.strftime("%Y%m%d"), end_de=end.strftime("%Y%m%d"), pblntf_ty="A", last_reprt_at="N", sort="date", sort_mth="desc", page_no="1", page_count="100")
    reports = []
    cumulative_by_year = {}
    seen = set()
    for row in data.get("list", []):
        code = disclosure_kind(row)
        if not code:
            continue
        year_match = re.search(r"20\d{2}", str(row.get("report_nm", "")))
        year = year_match.group(0) if year_match else None
        if not year or (year, code) in seen:
            continue
        seen.add((year, code))
        try:
            reprt_code = "11011" if code == "annual" else code
            financials = dart_json(key, "fnlttSinglAcntAll", corp_code=corp_code, bsns_year=year, reprt_code=reprt_code, fs_div="CFS")
            rows = financials.get("list", [])
            metrics = with_margin(metric_rows(rows))
            if metrics:
                quarter = {"11013": 1, "11012": 2, "11014": 3}.get(reprt_code)
                if code == "11014":
                    cumulative_by_year[int(year)] = metric_rows(rows, "thstrm_add_amount")
                if code == "11012":
                    period = f"{year} Q2"
                elif code == "11013":
                    period = f"{year} Q1"
                elif code == "11014":
                    period = f"{year} Q3"
                else:
                    period = f"{year} Q4"
                report = {"period": period, "year": int(year), "report_code": reprt_code, **metrics}
                if quarter:
                    report["quarter"] = quarter
                    report["cumulative"] = metric_rows(rows, "thstrm_add_amount") or metric_rows(rows)
                reports.append(report)
        except DartError:
            continue
    reports.sort(key=lambda item: (item["year"], item["period"]))

    # The annual report is the full-year cumulative figure. Derive Q4 for the
    # latest available annual year by subtracting its 9-month cumulative data.
    annual_rows = [r for r in reports if r.get("report_code") == "11011"]
    for annual in annual_rows:
        q3_cumulative = cumulative_by_year.get(annual["year"])
        if q3_cumulative:
            q4 = dict(annual)
            q4["period"] = f"{annual['year']} Q4"
            q4["report_code"] = "derived_q4"
            for key in ("revenue", "operating_income", "net_income"):
                q4[key] = round(annual[key] - q3_cumulative[key], 2) if annual.get(key) is not None and q3_cumulative.get(key) is not None else None
            q4["operating_income_pct"] = round(q4["operating_income"] / q4["revenue"] * 100, 1) if q4.get("operating_income") is not None and q4.get("revenue") not in (None, 0) else None
            reports.append(q4)
    reports = [r for r in reports if r.get("report_code") != "11011"]
    reports.sort(key=lambda item: (item["year"], item["period"]))
    return reports[-4:]


def current_year_estimate(annual, quarterly):
    year = date.today().year
    if any(row["year"] == year for row in annual):
        return None
    candidates = [row for row in quarterly if row["year"] == year and row.get("quarter")]
    if not candidates:
        return None
    latest = max(candidates, key=lambda row: row["quarter"])
    factor = 4 / latest["quarter"]
    cumulative = latest.get("cumulative", {})
    values = {key: round(cumulative[key] * factor, 2) if cumulative.get(key) is not None else None
              for key in ("revenue", "operating_income", "net_income")}
    if all(value is None for value in values.values()):
        return None
    return {"year": year, "estimated": True, "through_quarter": latest["quarter"], **with_margin(values)}


def company_data(key, corp_code):
    company = dart_json(key, "company", corp_code=corp_code)
    annual = annual_series(key, corp_code)
    quarterly = quarterly_series(key, corp_code)
    latest = annual[-1] if annual else {}
    employment = []
    if annual:
        year = str(latest["year"])
        try:
            employment = dart_json(key, "empSttus", corp_code=corp_code, bsns_year=year, reprt_code="11011").get("list", [])
        except DartError:
            employment = []
    return {"company": company, "annual": annual, "annual_estimate": current_year_estimate(annual, quarterly),
            "quarterly": quarterly, "employment": employment,
            "employment_year": latest.get("year"), "employment_summary": reported_employment_summary(key, employment)}


class Handler(BaseHTTPRequestHandler):
    corporations = []
    api_key = ""

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/heathz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if parsed.path in {"/", "/index.html", "/employees"}:
            body = (ROOT / ("employees.html" if parsed.path == "/employees" else "dashboard.html")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0].strip().casefold()
            matches = sorted(
                (row for row in self.corporations if query in row["corp_name"].casefold()),
                key=lambda row: (0 if row["corp_name"].casefold() == query else
                                 1 if row["corp_name"].casefold().startswith(query) else 2,
                                 row["corp_name"].casefold(), row["corp_code"]),
            )[:30] if query else []
            self.send_json(matches)
            return
        if parsed.path == "/api/company":
            code = parse_qs(parsed.query).get("corp_code", [""])[0]
            try:
                self.send_json(company_data(self.api_key, code))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if parsed.path == "/api/employment":
            params = parse_qs(parsed.query)
            code = params.get("corp_code", [""])[0]
            year = params.get("year", [""])[0]
            if not re.fullmatch(r"\d{8}", code) or not re.fullmatch(r"\d{4}", year):
                self.send_json({"error": "A corporation code and report year are required"}, 400)
                return
            try:
                rows = dart_json(self.api_key, "empSttus", corp_code=code, bsns_year=year, reprt_code="11011").get("list", [])
                self.send_json({"year": year, "rows": rows, "summary": reported_employment_summary(self.api_key, rows)})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if parsed.path == "/api/route-stream":
            global ROUTE_WORKER, ROUTE_WORKER_MTIME
            params = parse_qs(parsed.query)
            address = params.get("address", [""])[0].strip()
            start_address = params.get("start_address", [""])[0].strip()
            if not address:
                self.send_json({"error": "Destination address is required"}, 400)
                return
            if not start_address:
                self.send_json({"error": "Starting address is required"}, 400)
                return
            node = os.getenv("NAVER_NODE_PATH") or shutil.which("node") or ""
            playwright_candidates = [
                os.getenv("NAVER_PLAYWRIGHT_MODULE", ""),
                str(ROOT / "node_modules/playwright"),
            ]
            playwright = next((path for path in playwright_candidates if path and Path(path).exists()), "")
            if not node or not Path(playwright).exists():
                self.send_json({
                    "error": "Route service is unavailable: Node.js and Playwright are not installed on this instance"
                }, 503)
                return
            env = os.environ.copy()
            env["NAVER_PLAYWRIGHT_MODULE"] = playwright
            profile = Path(os.getenv("NAVER_PROFILE_PATH") or tempfile.gettempdir()) / f"corporation-survey-naver-{os.getpid()}"
            profile.mkdir(parents=True, exist_ok=True)
            env["NAVER_PROFILE_PATH"] = str(profile)
            with ROUTE_LOCK:
                try:
                    script_mtime = (ROOT / "naver_browser.js").stat().st_mtime_ns
                    if ROUTE_WORKER is not None and ROUTE_WORKER.poll() is None and ROUTE_WORKER_MTIME != script_mtime:
                        ROUTE_WORKER.stdin.close()
                        try:
                            ROUTE_WORKER.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            ROUTE_WORKER.terminate()
                            ROUTE_WORKER.wait(timeout=5)
                        ROUTE_WORKER = None
                    if ROUTE_WORKER is None or ROUTE_WORKER.poll() is not None:
                        ROUTE_WORKER = subprocess.Popen(
                            [node, str(ROOT / "naver_browser.js")],
                            cwd=ROOT, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                        )
                        ROUTE_WORKER_MTIME = script_mtime
                    ROUTE_WORKER.stdin.write((json.dumps({"address": address, "start_address": start_address}, ensure_ascii=False) + "\n").encode("utf-8"))
                    ROUTE_WORKER.stdin.flush()
                except Exception as exc:
                    self.send_json({"error": str(exc)}, 503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                received = False
                client_open = True
                for line in ROUTE_WORKER.stdout:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("type") == "ready":
                        continue
                    if event.get("type") == "complete":
                        break
                    received = True
                    if client_open:
                        try:
                            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            client_open = False
                if not received and client_open:
                    self.wfile.write(
                        b'{"type":"error","error":"Naver Maps browser exited before returning a route; check Playwright/Chromium installation"}\n'
                    )
                    self.wfile.flush()
            return
        self.send_error(404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default=os.getenv("OPENDART_API_KEY"))
    parser.add_argument("--corporations", default="temp/corporations.txt")
    default_port = int(os.getenv("PORT", "8000"))
    default_host = os.getenv("HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()
    if not args.api_key:
        parser.error("--api-key or OPENDART_API_KEY is required")
    corp_path = Path(args.corporations)
    if not corp_path.is_absolute():
        corp_path = ROOT / corp_path
    Handler.corporations = load_corporations(corp_path)
    Handler.api_key = args.api_key
    print(f"Loaded {len(Handler.corporations):,} corporations")
    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    print(f"Open http://{display_host}:{args.port} in your browser")
    DashboardServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
