"""데이터 출처. 각 함수는 [(date, value...)] 형태의 시계열을 돌려주거나 SourceError를 낸다.

출처마다 실패를 따로 처리해, 하나가 막혀도 나머지 지표는 계산되게 한다.
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
USER_AGENT = "Mozilla/5.0 (compatible; us-market-briefing/1.0; +https://github.com/)"
TIMEOUT = 30


class SourceError(Exception):
    pass


@dataclass
class Bar:
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float


def http_get(url: str, retries: int = 2) -> str:
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise SourceError(f"{url}: {last}")


# ---------- Yahoo Finance (비공식) ----------

def yahoo_url(symbol: str) -> str:
    sym = urllib.request.quote(symbol, safe="")
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1=0&period2={int(time.time()) + 86400}&interval=1d&events=history"


def parse_yahoo(text: str) -> list[Bar]:
    try:
        data = json.loads(text)
        res = data["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise SourceError(f"Yahoo 응답 형식이 예상과 다름: {e}")
    bars: dict[date, Bar] = {}
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        d = datetime.fromtimestamp(t, NY).date()
        bars[d] = Bar(d, q["open"][i], q["high"][i], q["low"][i], float(c))
    if not bars:
        raise SourceError("Yahoo 응답에 종가가 없음")
    return [bars[d] for d in sorted(bars)]


def yahoo(symbol: str) -> list[Bar]:
    return parse_yahoo(http_get(yahoo_url(symbol)))


# ---------- Stooq (대체 출처) ----------

def parse_stooq(text: str) -> list[Bar]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Close" not in rows[0]:
        raise SourceError("Stooq 응답 형식이 예상과 다름")
    out = []
    for r in rows:
        try:
            out.append(Bar(date.fromisoformat(r["Date"]), _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low")), float(r["Close"])))
        except (ValueError, TypeError):
            continue
    if not out:
        raise SourceError("Stooq 응답에 종가가 없음")
    return sorted(out, key=lambda b: b.date)


def stooq(symbol: str) -> list[Bar]:
    return parse_stooq(http_get(f"https://stooq.com/q/d/l/?s={urllib.request.quote(symbol, safe='')}&i=d"))


# ---------- CBOE VIX (대체 출처) ----------

def parse_cboe_vix(text: str) -> list[Bar]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "CLOSE" not in rows[0]:
        raise SourceError("CBOE 응답 형식이 예상과 다름")
    out = []
    for r in rows:
        try:
            d = datetime.strptime(r["DATE"], "%m/%d/%Y").date()
            out.append(Bar(d, _f(r.get("OPEN")), _f(r.get("HIGH")), _f(r.get("LOW")), float(r["CLOSE"])))
        except (ValueError, TypeError):
            continue
    if not out:
        raise SourceError("CBOE 응답에 종가가 없음")
    return sorted(out, key=lambda b: b.date)


def cboe_vix() -> list[Bar]:
    return parse_cboe_vix(http_get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"))


# ---------- FRED (미 연준, 공식) ----------

def parse_fred(text: str, series: str) -> list[tuple[date, float]]:
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2 or series not in rows[0]:
        raise SourceError(f"FRED {series} 응답 형식이 예상과 다름")
    col = rows[0].index(series)
    out = []
    for r in rows[1:]:
        try:
            out.append((date.fromisoformat(r[0]), float(r[col])))
        except (ValueError, IndexError):
            continue  # "." 또는 빈칸 = 휴일·미발표
    if not out:
        raise SourceError(f"FRED {series} 값이 없음")
    return out


def fred(series: str) -> list[tuple[date, float]]:
    return parse_fred(http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"), series)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def first_ok(*attempts):
    """(이름, 함수) 순서대로 시도해 처음 성공한 (이름, 결과)를 돌려준다."""
    errors = []
    for name, fn in attempts:
        try:
            return name, fn()
        except SourceError as e:
            errors.append(f"{name}: {e}")
    raise SourceError(" / ".join(errors))
