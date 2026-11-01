
import csv
import logging
import math
import os
import re
import time
from datetime import date, datetime, timezone

import requests


# ==========================================
# SETTINGS
# ==========================================

HALALWALLET_URL = (
    "https://www.halalwallet.us/api/data/"
    "halal-stock-screening-data-us.json"
)

MIZAN_URL = "https://askmizan.com/api/v1/screen"

APPROVED_FILE = "approved_halal.csv"
REPORT_FILE = "halal_candidates.csv"

STANDARD = "AAOIFI"

MAX_DATA_AGE_DAYS = int(
    os.getenv("MAX_DATA_AGE_DAYS", "365")
)

CHECK_LIMIT = int(
    os.getenv("HALAL_CHECK_LIMIT", "0")
)

REQUEST_TIMEOUT = 20
REQUEST_DELAY = 0.5


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)


# ==========================================
# SYMBOL VALIDATION
# ==========================================

def valid_symbol(symbol):

    return (
        isinstance(symbol, str)
        and re.fullmatch(
            r"[A-Z][A-Z0-9.-]{0,9}",
            symbol.upper()
        ) is not None
    )


# ==========================================
# DATE VALIDATION
# ==========================================

def is_recent(value):

    if not isinstance(value, str):
        return False

    try:

        data_date = date.fromisoformat(
            value[:10]
        )

        today = datetime.now(
            timezone.utc
        ).date()

        age = (today - data_date).days

        return (
            0 <= age <= MAX_DATA_AGE_DAYS
        )

    except ValueError:
        return False


# ==========================================
# NUMERIC VALIDATION
# ==========================================

def valid_percentage(value, maximum):

    if isinstance(value, bool):
        return False

    if not isinstance(value, (int, float)):
        return False

    if not math.isfinite(value):
        return False

    return 0 <= value <= maximum


# ==========================================
# HALALWALLET SCREENING
# ==========================================

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

    for field in required_flags:

        if row.get(field) is not True:
            return False

    percentage_limits = (
        ("debtPct", 30),
        ("cashPct", 30),
        ("impermissibleIncomePct", 5),
    )

    for field, limit in percentage_limits:

        if not valid_percentage(
            row.get(field),
            limit
        ):
            return False

    if not is_recent(
        row.get("dataAsOf")
    ):
        return False

    return True


# ==========================================
# MIZAN SCREENING
# ==========================================

def mizan_pass(data, symbol):

    if not isinstance(data, dict):
        return False

    api_symbol = str(
        data.get("ticker", "")
    ).upper()

    if api_symbol != symbol:
        return False

    if not is_recent(
        data.get("as_of")
    ):
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

    if business.get(
        "main_business"
    ) != "permitted":
        return False

    if summary.get(
        "business_screen"
    ) != "pass":
        return False

    by_standard = business.get(
        "by_standard"
    )

    if not isinstance(by_standard, dict):
        return False

    if by_standard.get(
        STANDARD
    ) != "pass":
        return False

    for screen in screens:

        if not isinstance(screen, dict):
            continue

        if screen.get(
            "standard"
        ) != STANDARD:
            continue

        return (
            screen.get("compliant") is True
            and screen.get("debt_compliant") is True
            and screen.get("borderline") is False
        )

    return False


# ==========================================
# FETCH HALALWALLET DATA
# ==========================================

def fetch_wallet(session):

    response = session.get(
        HALALWALLET_URL,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError(
            "Invalid HalalWallet response"
        )

    records = payload.get("records")

    if not isinstance(records, list):
        raise RuntimeError(
            "Missing HalalWallet records"
        )

    if not records:
        raise RuntimeError(
            "HalalWallet records are empty"
        )

    return records


# ==========================================
# FETCH MIZAN DATA
# ==========================================

def fetch_mizan(session, symbol):

    try:

        response = session.get(
            f"{MIZAN_URL}/{symbol}",
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 404:
            return None, "not_in_mizan"

        if response.status_code == 429:
            return None, "rate_limited"

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict):
            return None, "invalid_response"

        return data, None

    except requests.Timeout:
        return None, "timeout"

    except requests.RequestException as error:

        logger.warning(
            "%s API error: %s",
            symbol,
            error
        )

        return None, "api_error"

    except ValueError:
        return None, "invalid_json"


# ==========================================
# WRITE CSV
# ==========================================

def write_csv(path, header, rows):

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.writer(file)

        writer.writerow(header)
        writer.writerows(rows)


# ==========================================
# MAIN
# ==========================================

def main():

    logger.info(
        "Starting automatic halal screening"
    )

    session = requests.Session()

    session.headers.update({
        "User-Agent": "HalalStockSignals/1.0",
        "Accept": "application/json"
    })

    records = fetch_wallet(session)

    logger.info(
        "HalalWallet total records: %d",
        len(records)
    )

    # Select candidates from HalalWallet.
    # No Nasdaq Trader request is needed.
    candidates = {}

    for row in records:

        if not isinstance(row, dict):
            continue

        symbol = str(
            row.get("ticker") or ""
        ).strip().upper()

        if not valid_symbol(symbol):
            continue

        if wallet_pass(row):
            candidates[symbol] = row

    symbols = sorted(candidates)

    logger.info(
        "HalalWallet eligible candidates: %d",
        len(symbols)
    )

    if CHECK_LIMIT > 0:
        symbols = symbols[:CHECK_LIMIT]

    logger.info(
        "Mizan symbols to check: %d",
        len(symbols)
    )

    approved = []
    report = []

    checked_at = datetime.now(
        timezone.utc
    ).isoformat()

    for index, symbol in enumerate(
        symbols,
        start=1
    ):

        data, error = fetch_mizan(
            session,
            symbol
        )

        if error:
            status = error

        elif mizan_pass(data, symbol):

            status = "approved"
            approved.append(symbol)

        else:
            status = "mizan_not_approved"

        report.append([
            symbol,
            status,
            checked_at
        ])

        logger.info(
            "%d/%d %s: %s",
            index,
            len(symbols),
            symbol,
            status
        )

        if status == "rate_limited":

            logger.warning(
                "Mizan rate limit reached"
            )

            break

        time.sleep(REQUEST_DELAY)

    # Always replace the previous approved list.
    # Never retain stale approvals.
    write_csv(
        APPROVED_FILE,
        ["symbol"],
        [
            [symbol]
            for symbol in sorted(approved)
        ]
    )

    write_csv(
        REPORT_FILE,
        [
            "symbol",
            "status",
            "checked_at_utc"
        ],
        report
    )

    logger.info(
        "Screening completed"
    )

    logger.info(
        "Approved stocks: %d",
        len(approved)
    )

    logger.info(
        "Approved file: %s",
        APPROVED_FILE
    )

    logger.info(
        "Report file: %s",
        REPORT_FILE
    )

    if not approved:

        logger.warning(
            "No approved stocks. "
            "No trading signals allowed."
        )


if __name__ == "__main__":
    main()
