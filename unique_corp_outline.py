"""Keep the most recently updated row per 사업자등록번호 (bzno) from getCorpOutline.csv.

Recency = lastOpegDt (최종개방일자), then fssCorpChgDtm (금융감독원법인변경일시),
then fstOpegDt; if still tied, the later row in the file wins.
Rows with an empty bzno are kept as-is.

Usage: python unique_corp_outline.py [in.csv] [out.csv]
"""
import csv, sys

csv.field_size_limit(10**9)
src = sys.argv[1] if len(sys.argv) > 1 else "corp_data/getCorpOutline.csv"
dst = sys.argv[2] if len(sys.argv) > 2 else "corp_data/Unique_CorpOutline.csv"
RECENCY = ("lastOpegDt", "fssCorpChgDtm", "fstOpegDt")


def recency(row):
    return tuple((row.get(c) or "").strip() for c in RECENCY)


best, nobz, total = {}, [], 0
with open(src, encoding="utf-8-sig", newline="") as fi:
    reader = csv.DictReader(fi)
    fields = reader.fieldnames
    for row in reader:
        total += 1
        bz = (row.get("bzno") or "").strip()
        if not bz:
            nobz.append(row)
        elif bz not in best or recency(row) >= recency(best[bz]):
            best[bz] = row

with open(dst, "w", encoding="utf-8-sig", newline="") as fo:
    writer = csv.DictWriter(fo, fieldnames=fields)
    writer.writeheader()
    writer.writerows(best.values())
    writer.writerows(nobz)
kept = len(best) + len(nobz)
print(f"{total} rows read, {kept} kept ({total - kept} older duplicates removed) -> {dst}")
