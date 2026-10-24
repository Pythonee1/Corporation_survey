#!/usr/bin/env python3
"""Local Corporation Survey dashboard server."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from naver_route import START_ADDRESS

load_dotenv()

API = "https://opendart.fss.or.kr/api"
ROOT = Path(__file__).resolve().parent
ROUTE_LOCK = threading.Lock()
ROUTE_WORKER = None


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


def metric_rows(rows):
    patterns = {
        "revenue": ("매출액", "매출", "Revenue"),
        "operating_income": ("영업이익", "Operating income"),
        "net_income": ("당기순이익", "순이익", "Net income"),
        "assets": ("자산총계", "Total assets"),
        "liabilities": ("부채총계", "Total liabilities"),
        "equity": ("자본총계", "Total equity"),
    }
    result = {}
    for key, names in patterns.items():
        row = next((r for r in rows if any(name.lower() in str(r.get("account_nm", "")).lower() for name in names)), None)
        if row:
            raw_amount = number(row.get("thstrm_amount"))
            # Open DART returns these account amounts in KRW. The dashboard
            # presents every financial figure in million KRW.
            result[key] = round(raw_amount / 1_000_000, 2) if raw_amount is not None else None
    return result


def with_margin(metrics):
    revenue = metrics.get("revenue")
    operating = metrics.get("operating_income")
    metrics = dict(metrics)
    metrics["operating_income_pct"] = round(operating / revenue * 100, 2) if revenue else None
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
            metrics = with_margin(metric_rows(financials.get("list", [])))
            if metrics:
                if code == "11012":
                    period = f"{year} Q2"
                elif code == "11013":
                    period = f"{year} Q1"
                elif code == "11014":
                    period = f"{year} Q3"
                else:
                    period = f"{year} Q4"
                reports.append({"period": period, "year": int(year), "report_code": reprt_code, **metrics})
        except DartError:
            continue
    reports.sort(key=lambda item: (item["year"], item["period"]))

    # The annual report is the full-year cumulative figure. Derive Q4 for the
    # latest available annual year by subtracting its 9-month cumulative data.
    annual_rows = [r for r in reports if r.get("report_code") == "11011"]
    for annual in annual_rows:
        q3 = next((r for r in reports if r["year"] == annual["year"] and r.get("report_code") == "11014"), None)
        if q3:
            q4 = dict(annual)
            q4["period"] = f"{annual['year']} Q4"
            for key in ("revenue", "operating_income", "net_income"):
                if annual.get(key) is not None and q3.get(key) is not None:
                    q4[key] = annual[key] - q3[key]
            q4["operating_income_pct"] = round(q4["operating_income"] / q4["revenue"] * 100, 2) if q4.get("revenue") else None
            reports.append(q4)
    reports = [r for r in reports if r.get("report_code") != "11011"]
    reports.sort(key=lambda item: (item["year"], item["period"]))
    return reports[-4:]


def company_data(key, corp_code):
    company = dart_json(key, "company", corp_code=corp_code)
    annual = annual_series(key, corp_code)
    quarterly = quarterly_series(key, corp_code)
    latest = annual[-1] if annual else {}
    employment = {}
    if annual:
        year = str(latest["year"])
        try:
            employment = dart_json(key, "empSttus", corp_code=corp_code, bsns_year=year, reprt_code="11011").get("list", [])
        except DartError:
            employment = []
    return {"company": company, "annual": annual, "quarterly": quarterly, "employment": employment}


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
        if parsed.path in {"/", "/index.html"}:
            body = (ROOT / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0].strip().lower()
            matches = [row for row in self.corporations if query in row["corp_name"].lower()][:30] if query else []
            self.send_json(matches)
            return
        if parsed.path == "/api/company":
            code = parse_qs(parsed.query).get("corp_code", [""])[0]
            try:
                self.send_json(company_data(self.api_key, code))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if parsed.path == "/api/route-stream":
            global ROUTE_WORKER
            address = parse_qs(parsed.query).get("address", [""])[0].strip()
            if not address:
                self.send_json({"error": "Destination address is required"}, 400)
                return
            bundle = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
            node = os.getenv("NAVER_NODE_PATH") or str(bundle / "bin/node.exe")
            if not Path(node).is_file():
                node = shutil.which("node") or ""
            playwright = os.getenv("NAVER_PLAYWRIGHT_MODULE") or next(
                (str(path) for path in (bundle / "node_modules/playwright", ROOT / "node_modules/playwright") if path.exists()),
                "",
            )
            if not node or not Path(playwright).exists():
                self.send_json({"error": "Node.js and Playwright are required for Naver Maps browser automation"}, 503)
                return
            env = os.environ.copy()
            env["NAVER_PLAYWRIGHT_MODULE"] = playwright
            profile = Path(os.getenv("NAVER_PROFILE_PATH") or ROOT / "temp/naver-browser-profile")
            profile.mkdir(parents=True, exist_ok=True)
            env["NAVER_PROFILE_PATH"] = str(profile)
            with ROUTE_LOCK:
                try:
                    if ROUTE_WORKER is None or ROUTE_WORKER.poll() is not None:
                        ROUTE_WORKER = subprocess.Popen(
                            [node, str(ROOT / "naver_browser.js")],
                            cwd=ROOT, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        )
                    ROUTE_WORKER.stdin.write((json.dumps({"address": address, "start_address": START_ADDRESS}, ensure_ascii=False) + "\n").encode("utf-8"))
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
                    self.wfile.write(b'{"type":"error","error":"Naver Maps browser exited without a route"}\n')
                    self.wfile.flush()
            return
        self.send_error(404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default=os.getenv("OPENDART_API_KEY"))
    parser.add_argument("--corporations", default="temp/corporations.txt")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0", help="Use 127.0.0.1 to restrict access to this computer only")
    args = parser.parse_args()
    if not args.api_key:
        parser.error("--api-key or OPENDART_API_KEY is required")
    corp_path = Path(args.corporations)
    if not corp_path.is_absolute():
        corp_path = ROOT / corp_path
    Handler.corporations = load_corporations(corp_path)
    Handler.api_key = args.api_key
    print(f"Loaded {len(Handler.corporations):,} corporations")
    print(f"Open http://127.0.0.1:{args.port} on this computer")
    if args.host == "0.0.0.0":
        try:
            tailscale_ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()[0]
            print(f"Open http://{tailscale_ip}:{args.port} on your phone (Tailscale)")
        except (OSError, subprocess.SubprocessError, IndexError):
            print("Could not detect Tailscale IP; run 'tailscale ip -4' to find it")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
