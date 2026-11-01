
import csv
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

import requests


# ==========================================
# SETTINGS
# ==========================================

API_BASE = "https://askmizan.com/api/v1/screen"

INPUT_FILE = "us_stocks.csv"
CANDIDATES_FILE = "halal_candidates.csv"
APPROVED_FILE = "approved_halal.csv"

STANDARD = "AAOIFI"

CHECK_LIMIT = int(
    os.getenv("HALAL_CHECK_LIMIT", "20")
)

REQUEST_DELAY = 0.5
REQUEST_TIMEOUT = 15


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)


# ==========================================
# READ STOCK SYMBOLS
# ==========================================

def read_symbols():

    if not os.path.isfile(INPUT_FILE):
        raise FileNotFoundError(
            f"Missing file: {INPUT_FILE}"
        )

    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        lines = [
            line.strip()
            for line in file
            if line.strip()
        ]

    if not lines:
        return []

    header = lines[0].strip().lower()

    if header not in ("symbol", "ticker"):
        raise ValueError(
            "First line must be symbol or ticker"
        )

    symbols = set()

    for line in lines[1:]:

        symbol = (
            line.split(",")[0]
            .strip()
            .upper()
        )

        if re.fullmatch(
            r"[A-Z][A-Z0-9.-]{0,9}",
            symbol
        ):
            symbols.add(symbol)

    return sorted(symbols)


# ==========================================
# BUSINESS SCREENING
# ==========================================

def business_check(data):

    business = data.get("business", {})
    summary = data.get("summary", {})

    if not isinstance(business, dict):
        return False

    if not isinstance(summary, dict):
        return False

    main_business = business.get(
        "main_business"
    )

    business_screen = summary.get(
        "business_screen"
    )

    return (
        main_business == "permitted"
        and business_screen == "pass"
    )


# ==========================================
# AAOIFI DEBT SCREENING
# ==========================================

def debt_check(data):

    screens = data.get("screens", [])

    if not isinstance(screens, list):
        return False

    for screen in screens:

        if not isinstance(screen, dict):
            continue

        if screen.get("standard") != STANDARD:
            continue

        compliant = screen.get(
            "compliant"
        )

        borderline = screen.get(
            "borderline"
        )

        debt_compliant = screen.get(
            "debt_compliant"
        )

        return (
            compliant is True
            and borderline is False
            and debt_compliant is True
        )

    return False


# ==========================================
# NON-PERMISSIBLE INCOME CHECK
# ==========================================

def income_check(data):

    business = data.get("business", {})

    if not isinstance(business, dict):
        return False

    income_test = business.get(
        "income_test"
    )

    # Do not approve if income screening
    # has not been independently verified.
    return income_test == "pass"


# ==========================================
# EVALUATE STOCK
# ==========================================

def evaluate_stock(data):

    if not isinstance(data, dict):
        return "unknown"

    if not business_check(data):
        return "business_failed_or_unknown"

    if not debt_check(data):
        return "debt_failed_or_unknown"

    if not income_check(data):
        return "income_not_verified"

    return "fully_verified"


# ==========================================
# FETCH MIZAN API
# ==========================================

def fetch_screen(session, symbol):

    try:

        response = session.get(
            f"{API_BASE}/{symbol}",
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 404:
            return None, "not_covered"

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
            "%s request error: %s",
            symbol,
            error
        )

        return None, "request_error"

    except ValueError:
        return None, "invalid_json"


# ==========================================
# SAVE CANDIDATES
# ==========================================

def save_candidates(rows):

    with open(
        CANDIDATES_FILE,
        "w",
        encoding="utf-8",
        newline=""
    ) as file:

        writer = csv.writer(file)

        writer.writerow([
            "symbol",
            "status",
            "standard",
            "checked_at_utc"
        ])

        writer.writerows(rows)


# ==========================================
# MAIN
# ==========================================

def main():

    symbols = read_symbols()

    if CHECK_LIMIT > 0:
        symbols = symbols[:CHECK_LIMIT]

    logger.info(
        "Starting halal screening: %d symbols",
        len(symbols)
    )

    results = Counter()
    candidates = []

    checked_at = datetime.now(
        timezone.utc
    ).isoformat()

    with requests.Session() as session:

        session.headers.update({
            "User-Agent": "HalalStockSignals/1.0",
            "Accept": "application/json"
        })

        for index, symbol in enumerate(
            symbols,
            start=1
        ):

            data, error = fetch_screen(
                session,
                symbol
            )

            if error:
                status = error
            else:
                status = evaluate_stock(data)

            results[status] += 1

            logger.info(
                "%d/%d %s: %s",
                index,
                len(symbols),
                symbol,
                status
            )

            # These are preliminary candidates.
            # They are NOT fully approved stocks.
            if status in (
                "income_not_verified",
                "fully_verified"
            ):

                candidates.append([
                    symbol,
                    status,
                    STANDARD,
                    checked_at
                ])

            if status == "rate_limited":

                logger.warning(
                    "Mizan API rate limit reached."
                )

                break

            time.sleep(REQUEST_DELAY)

    save_candidates(candidates)

    logger.info(
        "Screening completed"
    )

    logger.info(
        "Results: %s",
        dict(results)
    )

    logger.info(
        "Candidates saved: %d",
        len(candidates)
    )

    logger.info(
        "Candidate file: %s",
        CANDIDATES_FILE
    )

    verified_count = results.get(
        "fully_verified",
        0
    )

    logger.info(
        "Fully verified: %d",
        verified_count
    )

    logger.info(
        "Approved file unchanged: %s",
        APPROVED_FILE
    )

    if verified_count == 0:

        logger.warning(
            "No fully verified stocks. "
            "Independent income screening "
            "is still required."
        )


if __name__ == "__main__":
    main()
