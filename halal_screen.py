
import csv
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


# ==========================================
# SETTINGS
# ==========================================

WALLET_URL = (
    "https://www.halalwallet.us/api/data/"
    "halal-stock-screening-data-us.json"
)

MIZAN_URL = "https://askmizan.com/api/v1/screen"

APPROVED_FILE = "approved_halal.csv"
REPORT_FILE = "halal_candidates.csv"
CACHE_FILE = Path(".cache/halal_mizan.json")

STANDARD = "AAOIFI"

MAX_WALLET_AGE_DAYS = 365
MAX_MIZAN_DATA_AGE_DAYS = 7

# Cache approved/rejected results for 7 days.
CACHE_TTL_HOURS = 168

# Scan up to 40 pending stocks every hour.
SCREEN_INTERVAL_HOURS = 1
MAX_REQUESTS_PER_BATCH = 40

# Delay between API requests.
REQUEST_DELAY_SECONDS = 2
TIMEOUT = 20


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger(__name__)


# ==========================================
# TIME HELPERS
# ==========================================

def utc_now():
    return datetime.now(timezone.utc)


def parse_date(value):
    try:
        if not isinstance(value, str):
            return None

        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def is_fresh(value, max_days):
    dt = parse_date(value)

    if dt is None:
        return False

    age = utc_now() - dt

    return (
        timedelta(0)
        <= age
        <= timedelta(days=max_days)
    )


# ==========================================
# VALIDATION
# ==========================================

def valid_symbol(value):
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"[A-Z][A-Z0-9.-]{0,9}",
            value.upper()
        ) is not None
    )


def valid_pct(value, maximum):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 <= value <= maximum
    )


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
        "filingVerified"
    )

    if not all(
        row.get(key) is True
        for key in required_flags
    ):
        return False

    limits = (
        ("debtPct", 30),
        ("cashPct", 30),
        ("impermissibleIncomePct", 5)
    )

    for key, limit in limits:
        if not valid_pct(
            row.get(key),
            limit
        ):
            return False

    return is_fresh(
        row.get("dataAsOf"),
        MAX_WALLET_AGE_DAYS
    )


# ==========================================
# MIZAN SCREENING
# ==========================================

def mizan_pass(data, symbol):
    if not isinstance(data, dict):
        return False

    if str(
        data.get("ticker", "")
    ).upper() != symbol:
        return False

    if not is_fresh(
        data.get("as_of"),
        MAX_MIZAN_DATA_AGE_DAYS
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

    standards = business.get("by_standard")

    if not isinstance(standards, dict):
        return False

    if business.get("main_business") != "permitted":
        return False

    if summary.get("business_screen") != "pass":
        return False

    if standards.get(STANDARD) != "pass":
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


# ==========================================
# CACHE
# ==========================================

def load_cache():
    if not CACHE_FILE.exists():
        return {
            "last_batch_at": None,
            "results": {}
        }

    try:
        with CACHE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if (
            isinstance(data, dict)
            and isinstance(
                data.get("results"),
                dict
            )
        ):
            return data

    except (OSError, ValueError):
        log.warning(
            "Invalid cache, starting fresh"
        )

    return {
        "last_batch_at": None,
        "results": {}
    }


def save_cache(cache):
    CACHE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = CACHE_FILE.with_suffix(".tmp")

    with temp.open(
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            cache,
            f,
            indent=2
        )

    os.replace(
        temp,
        CACHE_FILE
    )


def cache_valid(entry):
    if not isinstance(entry, dict):
        return False

    checked = parse_date(
        entry.get("checked_at")
    )

    if checked is None:
        return False

    age = utc_now() - checked

    return (
        timedelta(0)
        <= age
        <= timedelta(
            hours=CACHE_TTL_HOURS
        )
    )


# ==========================================
# API REQUESTS
# ==========================================

def fetch_wallet(session):
    response = session.get(
        WALLET_URL,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError(
            "Invalid HalalWallet response"
        )

    records = payload.get("records")

    if (
        not isinstance(records, list)
        or not records
    ):
        raise RuntimeError(
            "Invalid HalalWallet records"
        )

    return records


def fetch_mizan(session, symbol):
    try:
        response = session.get(
            f"{MIZAN_URL}/{symbol}",
            timeout=TIMEOUT
        )

        if response.status_code == 429:
            return "rate_limited", None

        if response.status_code in (404, 422):
            return "not_covered", None

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict):
            return "api_error", None

        if not is_fresh(
            data.get("as_of"),
            MAX_MIZAN_DATA_AGE_DAYS
        ):
            return "stale_mizan", None

        if mizan_pass(data, symbol):
            return (
                "approved",
                data.get("as_of")
            )

        return (
            "mizan_not_approved",
            data.get("as_of")
        )

    except (
        requests.RequestException,
        ValueError
    ) as exc:
        log.warning(
            "%s: %s",
            symbol,
            exc
        )

        return "api_error", None


# ==========================================
# CSV OUTPUT
# ==========================================

def write_csv(path, header, rows):
    with open(
        path,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


# ==========================================
# MAIN
# ==========================================

def main():
    session = requests.Session()

    session.headers.update({
        "User-Agent": "HalalStockSignals/1.0",
        "Accept": "application/json"
    })

    records = fetch_wallet(session)

    log.info(
        "HalalWallet records: %d",
        len(records)
    )

    candidates = {}

    for row in records:
        if not isinstance(row, dict):
            continue

        symbol = str(
            row.get("ticker") or ""
        ).strip().upper()

        if (
            valid_symbol(symbol)
            and wallet_pass(row)
        ):
            candidates[symbol] = row

    symbols = sorted(candidates)

    log.info(
        "Wallet candidates: %d",
        len(symbols)
    )

    cache = load_cache()
    results = cache["results"]

    last_batch = parse_date(
        cache.get("last_batch_at")
    )

    due = (
        last_batch is None
        or utc_now() - last_batch
        >= timedelta(
            hours=SCREEN_INTERVAL_HOURS
        )
    )

    requests_made = 0
    cache_changed = False

    if due:
        log.info(
            "Starting Mizan batch: max %d stocks",
            MAX_REQUESTS_PER_BATCH
        )

        pending = [
            symbol
            for symbol in symbols
            if not cache_valid(
                results.get(symbol)
            )
        ]

        log.info(
            "Pending Mizan checks: %d",
            len(pending)
        )

        for symbol in pending[
            :MAX_REQUESTS_PER_BATCH
        ]:
            status, source_date = fetch_mizan(
                session,
                symbol
            )

            requests_made += 1

            log.info(
                "%s: %s",
                symbol,
                status
            )

            if status == "rate_limited":
                log.warning(
                    "Mizan rate limit reached. "
                    "Stopping this batch."
                )
                break

            if status in (
                "approved",
                "mizan_not_approved",
                "not_covered"
            ):
                results[symbol] = {
                    "status": status,
                    "checked_at": (
                        utc_now().isoformat()
                    ),
                    "source_as_of": source_date
                }

                cache_changed = True

            time.sleep(
                REQUEST_DELAY_SECONDS
            )

        cache["last_batch_at"] = (
            utc_now().isoformat()
        )

        cache_changed = True

    else:
        log.info(
            "Using cached Mizan results; "
            "next batch is not due yet"
        )

    approved = []
    report = []

    checked_at = utc_now().isoformat()

    for symbol in symbols:
        entry = results.get(symbol)

        status = (
            entry.get("status")
            if cache_valid(entry)
            else "pending_or_expired"
        )

        if status == "approved":
            approved.append(symbol)

        report.append([
            symbol,
            status,
            checked_at
        ])

    write_csv(
        APPROVED_FILE,
        ["symbol"],
        [[symbol] for symbol in approved]
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

    if cache_changed:
        save_cache(cache)

    output_file = os.getenv(
        "GITHUB_OUTPUT"
    )

    if output_file:
        with open(
            output_file,
            "a",
            encoding="utf-8"
        ) as f:
            f.write(
                "cache_changed="
                + (
                    "true"
                    if cache_changed
                    else "false"
                )
                + "\n"
            )

    log.info(
        "Mizan requests this run: %d",
        requests_made
    )

    log.info(
        "Approved stocks: %d",
        len(approved)
    )

    log.info(
        "Pending/expired: %d",
        sum(
            1
            for row in report
            if row[1] == "pending_or_expired"
        )
    )

    if not approved:
        log.warning(
            "No valid approved stocks. "
            "Scanner should produce no signals."
        )


if __name__ == "__main__":
    main()
