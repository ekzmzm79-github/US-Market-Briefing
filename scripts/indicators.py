"""지표 계산. 모두 리스트를 받아 같은 길이의 리스트를 돌려준다(계산 불가 구간은 None)."""
from __future__ import annotations


def sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    out[n - 1] = e
    for i in range(n, len(xs)):
        e = xs[i] * k + e * (1 - k)
        out[i] = e
    return out


def macd(xs: list[float], fast=12, slow=26, signal=9):
    f, s = ema(xs, fast), ema(xs, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[float | None] = [None] * len(xs)
    if start is not None:
        tail = ema([v for v in line[start:]], signal)  # type: ignore[arg-type]
        sig[start:] = tail
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def rsi(xs: list[float], n=14) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if len(xs) <= n:
        return out
    gains = [max(xs[i] - xs[i - 1], 0) for i in range(1, len(xs))]
    losses = [max(xs[i - 1] - xs[i], 0) for i in range(1, len(xs))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = _rsi(ag, al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = _rsi(ag, al)
    return out


def _rsi(ag, al):
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def adx(high, low, close, n=14) -> list[float | None]:
    """Wilder ADX. 고가·저가가 없는 날이 있으면 그 이후부터 계산한다."""
    L = len(close)
    out: list[float | None] = [None] * L
    start = 0
    for i in range(L):
        if high[i] is None or low[i] is None:
            start = i + 1
    if L - start < 2 * n + 1:
        return out
    tr, pdm, mdm = [], [], []
    for i in range(start + 1, L):
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    atr, ap, am = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs = []
    for j in range(n - 1, len(tr)):
        if j >= n:
            atr = atr - atr / n + tr[j]
            ap = ap - ap / n + pdm[j]
            am = am - am / n + mdm[j]
        pdi = 100 * ap / atr if atr else 0.0
        mdi = 100 * am / atr if atr else 0.0
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
    a = sum(dxs[:n]) / n
    first = start + 1 + (n - 1) + (n - 1)
    out[first] = a
    for k in range(n, len(dxs)):
        a = (a * (n - 1) + dxs[k]) / n
        out[start + 1 + (n - 1) + k] = a
    return out


def streak(flags: list[bool | None]) -> list[int]:
    """각 날짜까지 True가 몇 일 연속인지. False·None이면 0."""
    out, c = [], 0
    for f in flags:
        c = c + 1 if f else 0
        out.append(c)
    return out
