
import csv
import io
import logging
import os
import re
import time
from datetime import datetime, timezone, date

import requests

WALLET_URL = (
    "https://www.halalwallet.us/api/data/"
    "halal-stock-screening-data-us.json"
)
MIZAN_URL = "https://askmizan.com/api/v1/screen"

NASDAQ_URL = (
    "https://www.nasdaqtrader.com/dynamic/"
    "SymDir/nasdaqlisted.txt"
)
OTHER_URL = (
    "https://www.nasdaqtrader.com/dynamic/"
    "SymDir/otherlisted.txt"
)

APPROVED_FILE = "approved_halal.csv"
REPORT_FILE = "halal_candidates.csv"

MAX_AGE_DAYS = 365
TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


def valid_symbol(value):
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"[A-Z][A-Z0-9.-]{0,9}",
            value.upper()
        ) is not None
    )


def fresh(value):
    if not isinstance(value, str):
        return False
    try:
        d = date.fromisoformat(value[:10])
        age = (datetime.now(timezone.utc).date() - d).days
        return 0 <= age <= MAX_AGE_DAYS
    except ValueError:
        return False


def fetch_listed(session):
    symbols = set()

    for url, field in (
        (NASDAQ_URL, "Symbol"),
        (OTHER_URL, "ACT Symbol"),
    ):
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()

        reader = csv.DictReader(
            io.StringIO(response.text),
            delimiter="|"
        )

        for row in reader:
            symbol = row.get(field, "").strip().upper()

            if not valid_symbol(symbol):
                continue

            if row.get("Test Issue", "N") == "Y":
                continue

            if row.get("ETF", "N") == "Y":
                continue

            symbols.add(symbol)

    if not symbols:
        raise RuntimeError("No exchange symbols downloaded")

    return symbols


def wallet_pass(row):
    if row.get("entityType") != "stock":
        return False

    if row.get("basis") != "AAOIFI":
        return False

    if row.get("verdict") != "halal":
        return False

    flags = (
        "businessActivityPass",
        "financialScreenPass",
        "debtPass",
        "cashPass",
        "impermissibleIncomePass",
        "filingVerified",
    )

    if not all(row.get(k) is True for k in flags):
        return False

    for key, limit in (
        ("debtPct", 30),
        ("cashPct", 30),
        ("impermissibleIncomePct", 5),
    ):
        value = row.get(key)

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= limit
        ):
            return False

    return fresh(row.get("dataAsOf"))


def mizan_pass(data, symbol):
    if not isinstance(data, dict):
        return False

    if str(data.get("ticker", "")).upper() != symbol:
        return False

    business = data.get("business", {})
    summary = data.get("summary", {})
    screens = data.get("screens", [])

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

    standards = business.get("by_standard", {})

    if not isinstance(standards, dict):
        return False

    if standards.get("AAOIFI") != "pass":
        return False

    if not fresh(data.get("as_of")):
        return False

    for screen in screens:
        if (
            isinstance(screen, dict)
            and screen.get("standard") == "AAOIFI"
        ):
            return (
                screen.get("compliant") is True
                and screen.get("debt_compliant") is True
                and screen.get("borderline") is False
            )

    return False


def write_csv(path, header, rows):
    with open(
        path, "w",
        newline="",
        encoding="utf-8"
    ) as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "HalalStockSignals/1.0",
        "Accept": "application/json",
    })

    listed = fetch_listed(session)
    log.info("US exchange symbols: %d", len(listed))

    response = session.get(WALLET_URL, timeout=TIMEOUT)
    response.raise_for_status()

    payload = response.json()
    records = payload.get("records")

    if not isinstance(records, list) or not records:
        raise RuntimeError("Invalid HalalWallet dataset")

    # Only currently listed, covered, verified stocks.
    candidates = {}

    for row in records:
        if not isinstance(row, dict):
            continue

        symbol = str(row.get("ticker") or "").upper()

        if (
            symbol in listed
            and wallet_pass(row)
        ):
            candidates[symbol] = row

    log.info(
        "HalalWallet eligible candidates: %d",
        len(candidates)
    )

    approved = []
    report = []

    checked_at = datetime.now(timezone.utc).isoformat()

    for index, symbol in enumerate(
        sorted(candidates), 1
    ):
        try:
            response = session.get(
                f"{MIZAN_URL}/{symbol}",
                timeout=15
            )

            if response.status_code == 404:
                status = "not_in_mizan"
            else:
                response.raise_for_status()

                if mizan_pass(response.json(), symbol):
                    status = "approved"
                    approved.append(symbol)
                else:
                    status = "mizan_not_approved"

        except (requests.RequestException, ValueError) as exc:
            log.warning("%s API error: %s", symbol, exc)
            status = "api_error"

        report.append([
            symbol,
            status,
            checked_at
        ])

        log.info(
            "%d/%d %s: %s",
            index,
            len(candidates),
            symbol,
            status
        )

        time.sleep(0.5)

    # Fail closed: never keep an old approval list.
    write_csv(
        APPROVED_FILE,
        ["symbol"],
        [[symbol] for symbol in approved]
    )

    write_csv(
        REPORT_FILE,
        ["symbol", "status", "checked_at_utc"],
        report
    )

    log.info("Approved stocks: %d", len(approved))
    log.info("Screening completed")

    if not approved:
        log.warning("No approved stocks. No signals allowed.")


if __name__ == "__main__":
    main()
