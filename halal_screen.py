
import csv
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

API = "https://askmizan.com/api/v1/screen"
OUTPUT = "approved_halal.csv"
INPUT = "us_stocks.csv"

# فحص AAOIFI فقط، مع رفض النتائج غير المؤكدة.
STANDARD = "aaoifi"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


def read_symbols():
    if not os.path.exists(INPUT):
        raise FileNotFoundError(
            f"{INPUT} not found"
        )

    with open(INPUT, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []

        symbol_column = next(
            (c for c in columns if c.lower() in
             ("symbol", "ticker")),
            None
        )

        if not symbol_column:
            raise ValueError(
                "CSV must contain symbol or ticker column"
            )

        symbols = set()

        for row in reader:
            symbol = (row.get(symbol_column) or "").strip().upper()

            if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
                symbols.add(symbol)

    return sorted(symbols)


def is_compliant(data):
    """
    Fail closed:
    لا نوافق إلا إذا ثبتت نتيجة AAOIFI صراحة.
    """

    if not isinstance(data, dict):
        return False

    # تختلف بنية الردود حسب إصدار الخدمة.
    # لا نخمن حالة النقاوة عند غياب الحقول.
    candidates = []

    standards = data.get("standards")

    if isinstance(standards, dict):
        candidates.append(
            standards.get("AAOIFI") or
            standards.get("aaoifi")
        )

    for key in ("aaoifi", "AAOIFI"):
        candidates.append(data.get(key))

    for item in candidates:
        if not isinstance(item, dict):
            continue

        compliant = item.get("compliant")
        borderline = item.get("borderline")

        if compliant is True and borderline is False:
            return True

    return False


def screen_symbol(session, symbol):
    try:
        response = session.get(
            f"{API}/{symbol}",
            timeout=15
        )

        if response.status_code == 404:
            return False, "not_covered"

        if response.status_code != 200:
            return False, f"http_{response.status_code}"

        data = response.json()

        if is_compliant(data):
            return True, "approved"

        return False, "not_approved_or_unknown"

    except (requests.RequestException, ValueError) as exc:
        logging.warning("%s: %s", symbol, exc)
        return False, "error"


def main():
    symbols = read_symbols()

    # حد تجريبي لتجنب آلاف الطلبات دفعة واحدة.
    limit = int(os.getenv("HALAL_CHECK_LIMIT", "20"))
    symbols = symbols[:max(0, limit)]

    logging.info("Screening %d symbols", len(symbols))

    approved = []
    results = {}

    with requests.Session() as session:
        session.headers.update({
            "User-Agent": "HalalStockSignals/1.0",
            "Accept": "application/json"
        })

        for index, symbol in enumerate(symbols, 1):
            allowed, status = screen_symbol(
                session, symbol
            )

            results[status] = results.get(status, 0) + 1

            if allowed:
                approved.append(symbol)

            logging.info(
                "%d/%d %s: %s",
                index, len(symbols), symbol, status
            )

            time.sleep(0.5)

    # لا نستبدل القائمة الحالية تلقائياً:
    # نتائج الاختبار قد تكون جزئية أو غير مكتملة.
    candidate_file = "halal_candidates.csv"

    with open(
        candidate_file, "w",
        newline="", encoding="utf-8"
    ) as f:
        writer = csv.writer(f)
        writer.writerow(["symbol"])
        for symbol in approved:
            writer.writerow([symbol])

    logging.info("Candidate symbols: %d", len(approved))
    logging.info("Results: %s", results)
    logging.info("Saved to %s", candidate_file)
    logging.info(
        "Review required before adding to %s",
        OUTPUT
    )


if __name__ == "__main__":
    main()
