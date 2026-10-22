"""Dump all three GetCorpBasicInfoService_V2 endpoints (금융위원회_기업기본정보) to CSV.

Usage:  set DATA_GO_KR_KEY=<인증키(Decoding)>  &&  python extract_corp_info.py [outdir]
Optional filters (e.g. basDt) can be added to PARAMS below.
"""
import csv, json, os, sys, time, urllib.parse, urllib.request
import os
from dotenv import load_dotenv

load_dotenv()

BASE = "http://apis.data.go.kr/1160100/service/GetCorpBasicInfoService_V2/"
ENDPOINTS = ["getCorpOutline_V2", "getAffiliate_V2", "getConsSubsComp_V2"]
PAGE_SIZE = 10000  # server-side maximum per the guide
PARAMS = {"basDt": "20261019"}       # e.g. {"basDt": "20260304"} to limit scope
MIN_EMPLOYEES = 100  # the API has no such filter, so it is applied client-side


 

def fetch(endpoint, page, key, retries=5):
    q = {"serviceKey": key, "resultType": "json", "numOfRows": PAGE_SIZE, "pageNo": page, **PARAMS}
    url = BASE + endpoint + "?" + urllib.parse.urlencode(q)
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                body = json.loads(r.read().decode("utf-8"))["response"]
            if body["header"]["resultCode"] != "00":
                raise RuntimeError(body["header"]["resultMsg"])
            return body["body"]
        except Exception as e:  # rate limit / transient backend errors
            print(f"  retry {i + 1}/{retries}: {e}", file=sys.stderr)
            time.sleep(2 ** i)
    raise SystemExit(f"Giving up on {endpoint} page {page}")


def keep(endpoint, item, crnos):
    if endpoint == "getCorpOutline_V2":
        try:
            ok = int(item.get("enpEmpeCnt") or 0) >= MIN_EMPLOYEES
        except ValueError:
            ok = False
        if ok:
            crnos.add(item["crno"])
        return ok
    return item.get("crno") in crnos  # affiliates/subsidiaries of qualifying companies only


def dump(endpoint, key, outdir, crnos):
    path = os.path.join(outdir, endpoint.replace("_V2", "") + ".csv")
    page, total, written, writer = 1, None, 0, None
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        while total is None or written < total:
            body = fetch(endpoint, page, key)
            total = int(body["totalCount"])
            items = (body.get("items") or {}).get("item") or []
            if isinstance(items, dict):
                items = [items]
            if not items:
                if written < total:
                    raise SystemExit(f"{endpoint}: empty page {page} after {written}/{total} rows (quota?)")
                break
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=list(items[0]), extrasaction="ignore")
                writer.writeheader()
            written += len(items)
            writer.writerows(i for i in items if keep(endpoint, i, crnos))
            print(f"{endpoint}: {written}/{total}")
            page += 1
    print("->", path)


if __name__ == "__main__":
    key = os.getenv("DATA_GO_KR_KEY") or sys.exit("Set DATA_GO_KR_KEY (decoded service key).")
    outdir = sys.argv[1] if len(sys.argv) > 1 else "corp_data"
    os.makedirs(outdir, exist_ok=True)
    crnos = set()  # filled by the outline endpoint, which must run first
    for ep in ENDPOINTS:
        dump(ep, key, outdir, crnos)
