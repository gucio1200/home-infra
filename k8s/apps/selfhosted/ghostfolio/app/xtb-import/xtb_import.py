import csv
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo(os.environ.get("TZ") or "Europe/Warsaw")
except Exception:
    LOCAL_TZ = timezone.utc

GHOSTFOLIO_URL = os.environ.get("GHOSTFOLIO_URL", "").rstrip("/")
GHOSTFOLIO_SECRET = os.environ.get("GHOSTFOLIO_SECRET", "")
INPUT_DIR = Path(os.environ.get("XTB_INPUT_DIR", "/data/import"))
OUTPUT_DIR = Path(os.environ.get("XTB_OUTPUT_DIR", "/data/przetworzone"))
STATE_FILE = Path(os.environ.get("XTB_STATE_FILE") or OUTPUT_DIR / ".xtb-state.json")
DRY_RUN = os.environ.get("XTB_DRY_RUN", "").lower() in ("1", "true", "yes")
MIN_FILE_AGE = float(os.environ.get("XTB_MIN_FILE_AGE_SECONDS", "30"))
FX_FEE_RATE = float(os.environ.get("XTB_FX_FEE_RATE", "0.005"))
SYMBOL_OVERRIDES = json.loads(os.environ.get("XTB_SYMBOL_OVERRIDES") or "{}")

EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)
XLSX_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
SUFFIXES = {
    "US": "",
    "UK": ".L",
    "DE": ".DE",
    "PL": ".WA",
    "FR": ".PA",
    "NL": ".AS",
    "ES": ".MC",
    "IT": ".MI",
    "PT": ".LS",
    "BE": ".BR",
    "CH": ".SW",
    "DK": ".CO",
    "SE": ".ST",
    "FI": ".HE",
    "NO": ".OL",
    "AT": ".VI",
    "IE": ".IR",
    "CZ": ".PR",
}
HEADER_ALIASES = {
    "id": ("id",),
    "type": ("type",),
    "time": ("time",),
    "ticker": ("ticker", "symbol"),
    "amount": ("amount",),
    "comment": ("comment",),
}
TRADE_COMMENT = re.compile(
    r"^(?:OPEN|CLOSE)\s+(?:BUY|SELL)\s+(?:(?P<ticker>\S+)\s+)?"
    r"(?P<quantity>\d+(?:\.\d+)?)(?:/\d+(?:\.\d+)?)?\s+@\s+(?P<price>\d+(?:\.\d+)?)"
)
FILE_NAME = re.compile(r"^(?:\d{8}-\d{6}_)?(?P<currency>[A-Z]{3})_(?P<account>\d+)_")


class XtbError(Exception):
    pass


class SemicolonDialect(csv.excel):
    delimiter = ";"


def api(method, path, body=None, token=None, timeout=300):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(GHOSTFOLIO_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        if error.code >= 500 or error.code == 429:
            raise RuntimeError(f"{method} {path}: HTTP {error.code} {detail}") from None
        raise XtbError(f"{method} {path}: HTTP {error.code} {detail}") from None
    return json.loads(raw) if raw else None


def login():
    if not GHOSTFOLIO_URL or not GHOSTFOLIO_SECRET:
        raise XtbError("GHOSTFOLIO_URL i GHOSTFOLIO_SECRET muszą być ustawione")
    return api("POST", "/api/v1/auth/anonymous", {"accessToken": GHOSTFOLIO_SECRET})["authToken"]


def xlsx_sheets(path):
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", XLSX_NS):
                shared.append("".join(text.text or "" for text in item.iter(f"{{{XLSX_NS['m']}}}t")))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.get("Id"): rel.get("Target") for rel in relations.findall("pr:Relationship", XLSX_NS)}
        sheets = []
        for sheet in workbook.find("m:sheets", XLSX_NS).findall("m:sheet", XLSX_NS):
            target = targets[sheet.get(f"{{{XLSX_NS['r']}}}id")].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            rows = []
            for row in ET.fromstring(archive.read(target)).iter(f"{{{XLSX_NS['m']}}}row"):
                cells = {}
                for cell in row.findall("m:c", XLSX_NS):
                    letters = "".join(ch for ch in cell.get("r", "") if ch.isalpha())
                    column = 0
                    for ch in letters:
                        column = column * 26 + ord(ch.upper()) - 64
                    value = cell.find("m:v", XLSX_NS)
                    kind = cell.get("t")
                    if kind == "s" and value is not None:
                        text = shared[int(value.text)]
                    elif kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter(f"{{{XLSX_NS['m']}}}t"))
                    else:
                        text = value.text if value is not None else ""
                    cells[column - 1] = text or ""
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            sheets.append((sheet.get("name") or "", rows))
    return sheets


def csv_rows(path):
    text = path.read_bytes().decode("utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
    except csv.Error:
        dialect = SemicolonDialect
    return list(csv.reader(io.StringIO(text), dialect))


def find_header(rows):
    for index, row in enumerate(rows):
        names = [cell.strip().lower() for cell in row]
        columns = {}
        for field, aliases in HEADER_ALIASES.items():
            for alias in aliases:
                if alias in names:
                    columns[field] = names.index(alias)
                    break
        if {"id", "type", "time", "amount", "comment"} <= columns.keys():
            return index, columns
    return None, None


def parse_time(value):
    value = value.strip()
    try:
        return EXCEL_EPOCH + timedelta(seconds=round(float(value) * 86400))
    except ValueError:
        pass
    for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise XtbError(f"nieznany format czasu {value!r}")


def parse_amount(value):
    return float(value.strip().replace(" ", "").replace(" ", "").replace(",", "."))


def read_export(path):
    match = FILE_NAME.match(path.name)
    account_number = match.group("account") if match else None
    currency = match.group("currency") if match else None
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        if not zipfile.is_zipfile(path):
            raise XtbError("plik .xlsx nie jest prawidłowym arkuszem Excela")
        sheets = xlsx_sheets(path)
        sheets.sort(key=lambda item: "cash" not in item[0].lower())
    elif suffix == ".csv":
        sheets = [(path.name, csv_rows(path))]
    else:
        raise XtbError("to nie jest eksport XTB: wrzuć plik .xlsx albo .csv z xStation (Historia konta → Operacje gotówkowe → Eksport)")
    for _, rows in sheets:
        for row in rows[:5]:
            if len(row) > 1 and row[0].strip().lower() == "account number":
                number = row[1].strip().split(".")[0]
                if account_number and number and number != account_number:
                    raise XtbError(f"numer rachunku w pliku ({number}) nie zgadza się z nazwą pliku ({account_number})")
                account_number = account_number or number
        start, columns = find_header(rows)
        if start is None:
            continue
        records = []
        for row in rows[start + 1:]:
            row = row + [""] * (max(columns.values()) + 1 - len(row))
            raw_id = row[columns["id"]].strip()
            if not raw_id:
                continue
            records.append(
                {
                    "id": int(float(raw_id)),
                    "type": row[columns["type"]].strip(),
                    "time": parse_time(row[columns["time"]]),
                    "ticker": row[columns["ticker"]].strip() if "ticker" in columns else "",
                    "amount": parse_amount(row[columns["amount"]]),
                    "comment": row[columns["comment"]].strip(),
                }
            )
        return account_number, currency, records
    raise XtbError("nie znalazłem tabeli operacji gotówkowych (kolumny Type, Time, Amount, ID, Comment)")


def classify(record):
    kind = record["type"].lower()
    if "purchase" in kind:
        return "BUY"
    if "sale" in kind:
        return "SELL"
    if kind.startswith("deposit") or "withdraw" in kind or "transfer" in kind:
        return "CASH"
    raise XtbError(
        f"operacja ID {record['id']} ma typ {record['type']!r}, którego importer jeszcze nie obsługuje; "
        "nic z tego pliku nie zostało zapisane"
    )


def yahoo_symbol(ticker):
    if ticker in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[ticker]
    base, _, market = ticker.rpartition(".")
    if not base or market not in SUFFIXES:
        raise XtbError(f"nie umiem przełożyć tickera XTB {ticker!r} na symbol Yahoo; dodaj go do XTB_SYMBOL_OVERRIDES")
    return base + SUFFIXES[market]


def make_resolver(token):
    cache = {}

    def resolve(ticker):
        if ticker not in cache:
            symbol = yahoo_symbol(ticker)
            try:
                quote = api("GET", f"/api/v1/symbols/YAHOO/{urllib.parse.quote(symbol, safe='')}", token=token)
            except XtbError as error:
                raise XtbError(f"Ghostfolio nie zna symbolu {symbol} (XTB {ticker}): {error}") from None
            currency = (quote or {}).get("currency")
            if not currency:
                raise XtbError(f"Ghostfolio nie podał waluty dla {symbol} (XTB {ticker})")
            if currency == "GBp":
                raise XtbError(f"{ticker} jest notowany w pensach (GBp); takie pozycje trzeba najpierw sprawdzić ręcznie")
            cache[ticker] = (symbol, currency)
        return cache[ticker]

    return resolve


def build_activity(record, side, account, resolve):
    match = TRADE_COMMENT.match(record["comment"])
    if not match:
        raise XtbError(f"operacja ID {record['id']}: nie rozumiem komentarza {record['comment']!r}")
    ticker = record["ticker"] or match.group("ticker") or ""
    if not ticker:
        raise XtbError(f"operacja ID {record['id']}: brak tickera")
    if (side == "BUY" and record["amount"] > 0) or (side == "SELL" and record["amount"] < 0):
        raise XtbError(f"operacja ID {record['id']}: kwota {record['amount']} nie pasuje do typu {record['type']!r}")
    symbol, currency = resolve(ticker)
    quantity = float(match.group("quantity"))
    price = float(match.group("price"))
    fee = round(quantity * price * FX_FEE_RATE, 6) if currency != account["currency"] else 0.0
    return {
        "accountId": account["id"],
        "comment": f"XTB {record['id']} - {record['comment']}",
        "currency": currency,
        "dataSource": "YAHOO",
        "date": record["time"].strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "fee": fee,
        "quantity": quantity,
        "symbol": symbol,
        "type": side,
        "unitPrice": price,
    }


def pick_account(accounts, number, currency):
    if number:
        by_comment = [a for a in accounts if number in (a.get("comment") or "")]
        if len(by_comment) == 1:
            return by_comment[0]
    candidates = [
        a
        for a in accounts
        if "xtb" in (a.get("name") or "").lower() or "xtb" in ((a.get("platform") or {}).get("name") or "").lower()
    ]
    if currency:
        candidates = [a for a in candidates if a.get("currency") == currency]
    if len(candidates) == 1:
        return candidates[0]
    raise XtbError(
        f"nie wiem, do którego konta w Ghostfolio przypisać rachunek XTB {number or '?'} ({currency or '?'}); "
        "wpisz numer rachunku XTB w komentarzu właściwego konta w Ghostfolio"
    )


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {"accounts": {}}


def save_state(state):
    temporary = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True))
    temporary.replace(STATE_FILE)


def process(path, token, accounts, state):
    number, currency, records = read_export(path)
    account = pick_account(accounts, number, currency)
    key = number or account["id"]
    known = set(state["accounts"].get(key, {}).get("ids", []))
    new = [record for record in records if record["id"] not in known]
    resolve = make_resolver(token)
    activities = []
    for record in new:
        side = classify(record)
        if side in ("BUY", "SELL"):
            activities.append(build_activity(record, side, account, resolve))
    delta = round(sum(record["amount"] for record in new), 2)
    balance = round(float(account.get("balance") or 0) + delta, 2)
    imported = 0
    if DRY_RUN:
        print(json.dumps(activities, indent=2, ensure_ascii=False))
    else:
        if activities:
            response = api("POST", "/api/v1/import", {"activities": activities}, token)
            imported = len((response or {}).get("activities") or [])
        if new:
            day = min(max(r["time"] for r in new).astimezone(LOCAL_TZ).date(), datetime.now(LOCAL_TZ).date())
            api("POST", f"/api/v1/accounts/{account['id']}/balances", {"balance": balance, "date": day.isoformat()}, token)
            account["balance"] = balance
        entry = state["accounts"].setdefault(key, {})
        entry["accountId"] = account["id"]
        entry["ids"] = sorted(known | {record["id"] for record in new})
        save_state(state)
    skipped = len(activities) - imported if not DRY_RUN else 0
    return (
        f"OK, konto {account['name']}: operacji w pliku {len(records)}, nowych {len(new)}, "
        f"transakcji zaimportowanych {imported}, pominiętych jako duplikaty {skipped}, "
        f"zmiana gotówki {delta:+.2f} {account['currency']}, saldo gotówki {balance:.2f} {account['currency']}"
    )


def archive(path, stamp, note):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_DIR / f"{stamp}_{path.name}"
    shutil.copyfile(path, target)
    path.unlink()
    (OUTPUT_DIR / f"{target.name}.wynik.txt").write_text(note + "\n")


def main():
    os.umask(0o002)
    now = time.time()
    files = [
        p
        for p in sorted(INPUT_DIR.iterdir())
        if p.is_file() and not p.name.startswith((".", "~$")) and now - p.stat().st_mtime >= MIN_FILE_AGE
    ]
    if not files:
        print("Brak plików do importu")
        return 0
    token = login()
    accounts = api("GET", "/api/v1/accounts", token=token)["accounts"]
    state = load_state()
    failures = 0
    for path in files:
        stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
        try:
            note = process(path, token, accounts, state)
        except (XtbError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, UnicodeDecodeError) as error:
            failures += 1
            note = f"BŁĄD: {error}"
        print(f"{path.name}: {note}", flush=True)
        if not DRY_RUN:
            archive(path, stamp, note)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
