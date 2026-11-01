
import csv
import logging
import os
import re
import time
from datetime import date, datetime, timezone

import requests

MIZAN_URL = "https://askmizan.com/api/v1/screen"
HALALWALLET_URL = (
    "https://www.halalwallet.us/api/data/"
    "halal-stock-screening-data-us.json"
)

INPUT_FILE = "us_stocks.csv"
APPROVED_FILE = "approved_halal.csv"
REPORT_FILE = "halal_candidates.csv"

STANDARD = "AAOIFI"
CHECK_LIMIT = int(os.getenv("HALAL_CHECK_LIMIT", "0"))
MAX_DATA_AGE_DAYS = 365

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


def read_symbols():
    with open(INPUT_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "symbol" not in reader.fieldnames:
            raise ValueError("us_stocks.csv needs a symbol column")

        symbols = {
            row["symbol"].strip().upper()
            for row in reader
            if row.get("symbol")
        }

    symbols = sorted(
        s for s in symbols
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", s)
    )

    return symbols[:CHECK_LIMIT] if CHECK_LIMIT > 0 else symbols


def recent_date(value):
    if not isinstance(value, str):
        return False

    try:
        checked = date.fromisoformat(value[:10])
        age = (datetime.now(timezone.utc).date() - checked).days
        return 0 <= age <= MAX_DATA_AGE_DAYS
    except ValueError:
        return False


def wallet_pass(row):
    if not isinstance(row, dict):
        return False

    if row.get("entityType") != "stock":
        return False

    if row.get("basis") != STANDARD:
        return False

    if row.get("verdict") != "halal":
        return False

    required_flags = (
        "businessActivityPass",
        "financialScreenPass",
        "debtPass",
        "cashPass",
        "impermissibleIncomePass",
        "filingVerified",
    )

    if not all(row.get(key) is True for key in required_flags):
        return False

    limits = (
        ("debtPct", 30),
        ("cashPct", 30),
        ("impermissibleIncomePct", 5),
    )

    for key, maximum in limits:
        value = row.get(key)

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False

        if not 0 <= value <= maximum:
            return False

    return recent_date(row.get("dataAsOf"))


def mizan_pass(data):
    if not isinstance(data, dict):
        return False

    business = data.get("business")
    summary = data.get("summary")
    screens = data.get("screens")

    if not isinstance(business, dict):
        return False

    if not isinstance(summary, dict):
        return False

    if not isinstance(screens, list):
        return False

    if business.get("main_business") != "permitted":
        return False

    if summary.get("business_screen") != "pass":
        return False

    if business.get("by_standard", {}).get(STANDARD) != "pass":
        return False

    for screen in screens:
        if not isinstance(screen, dict):
            continue

        if screen.get("standard") == STANDARD:
            return (
                screen.get("compliant") is True
                and screen.get("debt_compliant") is True
                and screen.get("borderline") is False
            )

    return False


def main():
    symbols = read_symbols()
    log.info("Checking %d stocks", len(symbols))

    session = requests.Session()
    session.headers.update({
        "User-Agent": "HalalStockSignals/1.0",
        "Accept": "application/json",
    })

    response = session.get(HALALWALLET_URL, timeout=30)
    response.raise_for_status()

    payload = response.json()
    records = payload.get("records")

    if not isinstance(records, list) or not records:
        raise RuntimeError("Invalid HalalWallet dataset")

    wallet = {
        row["ticker"].upper(): row
        for row in records
        if isinstance(row, dict)
        and row.get("entityType") == "stock"
        and isinstance(row.get("ticker"), str)
    }

    approved = []
    report = []
    checked_at = datetime.now(timezone.utc).isoformat()

    for i, symbol in enumerate(symbols, 1):
        row = wallet.get(symbol)

        if row is None:
            status = "not_in_halalwallet"
        elif not wallet_pass(row):
            status = "wallet_not_approved"
        else:
            try:
                r = session.get(
                    f"{MIZAN_URL}/{symbol}",
                    timeout=15
                )

                if r.status_code == 404:
                    status = "not_in_mizan"
                else:
                    r.raise_for_status()
                    data = r.json()

                    if (
                        isinstance(data, dict)
                        and data.get("ticker", "").upper() == symbol
                        and mizan_pass(data)
                    ):
                        status = "approved"
                        approved.append(symbol)
                    else:
                        status = "mizan_not_approved"

            except (requests.RequestException, ValueError) as exc:
                log.warning("%s: %s", symbol, exc)
                status = "api_error"

            time.sleep(0.5)

        report.append([symbol, status, checked_at])
        log.info("%d/%d %s: %s", i, len(symbols), symbol, status)

    # Write files only after screening has finished.
    with open(APPROVED_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol"])
        writer.writerows([[symbol] for symbol in approved])

    with open(REPORT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "status", "checked_at_utc"])
        writer.writerows(report)

    log.info("Approved stocks: %d", len(approved))
    log.info("Approved file: %s", APPROVED_FILE)

    if not approved:
        log.warning("No stocks approved; scanner must skip trading signals.")


if __name__ == "__main__":
    main()
