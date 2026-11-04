"""데이터를 받아 지표와 상태를 계산하고 data/ 아래 JSON을 쓴다.

    python scripts/compute.py              # 실제 출처에서 받아 계산
    python scripts/compute.py --offline DIR  # DIR의 저장된 응답으로 계산 (테스트용)

S&P 500 종가를 못 받으면 아무것도 덮어쓰지 않고 data/status.json에 실패를 남긴 뒤 1로 끝난다.
다른 출처가 실패하면 직전 값을 "stale"로 표시해 이어 쓴다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indicators as ind  # noqa: E402
import sources as src  # noqa: E402
import state as stt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = ZoneInfo("Asia/Seoul")
NY = src.NY
FRED_SERIES = {"us10y": "DGS10", "us2y": "DGS2", "spread_10y2y": "T10Y2Y", "hy_spread": "BAMLH0A0HYM2"}


def r(x, n=2):
    return None if x is None else round(x, n)


def load_rules() -> dict:
    return yaml.safe_load((ROOT / "config/rules.yaml").read_text(encoding="utf-8"))


# ---------- 수집 ----------

def fetch_all(rules, offline: Path | None):
    """{key: (출처 이름, 결과) 또는 SourceError}"""
    sym = rules["index"]["symbol"]
    if offline:
        def rd(name):
            p = offline / name
            if not p.exists():
                raise src.SourceError(f"{name} 없음")
            return p.read_text(encoding="utf-8")
        jobs = {
            "index": lambda: ("offline", src.parse_yahoo(rd("gspc.json"))),
            "vix": lambda: ("offline", src.parse_yahoo(rd("vix.json"))),
            "dxy": lambda: ("offline", src.parse_yahoo(rd("dxy.json"))),
            **{k: (lambda s=s: ("offline", src.parse_fred(rd(f"{s}.csv"), s))) for k, s in FRED_SERIES.items()},
        }
    else:
        jobs = {
            "index": lambda: src.first_ok(("Yahoo Finance", lambda: src.yahoo(sym)), ("Stooq", lambda: src.stooq("^spx"))),
            "vix": lambda: src.first_ok(("Yahoo Finance", lambda: src.yahoo("^VIX")), ("CBOE", src.cboe_vix)),
            "dxy": lambda: src.first_ok(("Yahoo Finance", lambda: src.yahoo("DX-Y.NYB"))),
            **{k: (lambda s=s: ("FRED", src.fred(s))) for k, s in FRED_SERIES.items()},
        }
    out = {}
    for k, fn in jobs.items():
        try:
            out[k] = fn()
        except src.SourceError as e:
            out[k] = e
    return out


def drop_incomplete(bars, now_ny: datetime):
    """장중에 실행하면 오늘 막대가 미완성이라 뺀다."""
    if bars and bars[-1].date == now_ny.date() and (now_ny.hour, now_ny.minute) < (16, 30):
        return bars[:-1]
    return bars


# ---------- 계산 ----------

def last_cross(sma50, sma200, dates):
    prev = None
    found = None
    for i, (a, b) in enumerate(zip(sma50, sma200)):
        if a is None or b is None:
            continue
        sign = a > b
        if prev is not None and sign != prev:
            found = {"type": "골든크로스" if sign else "데드크로스", "date": dates[i].isoformat()}
        prev = sign
    return found


def vix_band(v, rules):
    if v is None:
        return None
    if v <= rules["vix"]["calm_max"]:
        return "평온"
    if v >= rules["vix"]["fear_min"]:
        return "공포"
    return "보통"


def series_block(pts, n_change=20, pct=False):
    if not pts:
        return None
    d, v = pts[-1]
    out = {"value": r(v, 3), "date": d.isoformat()}
    if len(pts) > n_change:
        base = pts[-1 - n_change][1]
        out["chg_20d"] = r((v / base - 1) * 100 if pct else v - base, 3)
    if len(pts) > 1:
        base = pts[-2][1]
        out["chg_1d"] = r((v / base - 1) * 100 if pct else v - base, 3)
    return out


def compute(fetched, rules, now_utc: datetime, prev_latest: dict | None):
    now_ny = now_utc.astimezone(NY)
    status = {}
    for k, v in fetched.items():
        status[k] = {"ok": False, "error": str(v)} if isinstance(v, Exception) else {"ok": True, "source": v[0]}

    if isinstance(fetched["index"], Exception):
        raise src.SourceError(f"지수 종가를 받지 못함: {fetched['index']}")

    bars = drop_incomplete(fetched["index"][1], now_ny)
    dates = [b.date for b in bars]
    closes = [b.close for b in bars]
    status["index"]["last_date"] = dates[-1].isoformat()

    vix_map = {}
    if not isinstance(fetched["vix"], Exception):
        vb = drop_incomplete(fetched["vix"][1], now_ny)
        vix_map = {b.date: b.close for b in vb}
        status["vix"]["last_date"] = vb[-1].date.isoformat() if vb else None
    vix = [vix_map.get(d) for d in dates]

    sma50, sma200 = ind.sma(closes, 50), ind.sma(closes, 200)
    m_line, m_sig, m_hist = ind.macd(closes)
    rsi14 = ind.rsi(closes)
    adx14 = ind.adx([b.high for b in bars], [b.low for b in bars], closes)
    days = stt.classify(dates, closes, sma50, sma200, vix, rules)
    t = days[-1]
    i = len(days) - 1

    # 상태가 언제부터였나
    j = i
    while j > 0 and days[j - 1].state == t.state:
        j -= 1
    prev_state = days[j - 1].state if j > 0 else None

    st = rules["state"]
    above200 = [s is not None and c >= s for c, s in zip(closes, sma200)]
    above50 = [s is not None and c >= s for c, s in zip(closes, sma50)]
    down = [k > 0 and closes[k] < closes[k - 1] for k in range(len(closes))]

    index_block = {
        "name": rules["index"]["name"],
        "date": t.date.isoformat(),
        "close": r(t.close),
        "chg_1d_pct": r((t.close / closes[-2] - 1) * 100),
        "ath": r(t.ath), "ath_date": t.ath_date.isoformat(), "dd_ath": r(t.dd_ath),
        "new_ath_today": t.ath_date == t.date and i > 0 and days[i - 1].ath < t.close,
        "cycle_peak": r(t.cycle_peak), "cycle_peak_date": t.cycle_peak_date.isoformat(), "dd_cycle": r(t.dd_cycle),
        "to_correction_pp": r(t.dd_cycle - st["correction_pct"]) if t.dd_cycle > st["correction_pct"] else None,
        "to_bear_pp": r(t.dd_cycle - st["bear_pct"]) if t.dd_cycle > st["bear_pct"] else None,
        "down_streak": ind.streak(down)[-1],
    }
    if t.state == stt.BEAR:
        need = t.trough * (1 + st["bull_rebound_pct"] / 100)
        index_block["bear"] = {"trough": r(t.trough), "trough_date": t.trough_date.isoformat(),
                               "exit_level": r(need), "to_exit_pct": r((need / t.close - 1) * 100),
                               "from_trough_pct": r((t.close / t.trough - 1) * 100)}

    a200, a50 = ind.streak(above200), ind.streak(above50)
    b200 = ind.streak([not x for x in above200])
    b50 = ind.streak([not x for x in above50])
    trend = {
        "sma50": r(sma50[i]), "sma200": r(sma200[i]),
        "above_sma50": above50[i], "sma50_streak": a50[i] if above50[i] else b50[i],
        "above_sma200": above200[i], "sma200_streak": a200[i] if above200[i] else b200[i],
        "gap_sma200_pct": r((t.close / sma200[i] - 1) * 100) if sma200[i] else None,
        "sma50_above_sma200": (sma50[i] > sma200[i]) if sma50[i] and sma200[i] else None,
        "last_cross": last_cross(sma50, sma200, dates),
    }
    momentum = {"rsi14": r(rsi14[i], 1), "macd": r(m_line[i]), "macd_signal": r(m_sig[i]), "macd_hist": r(m_hist[i]),
                "adx14": r(adx14[i], 1)}

    v_now = vix[i]
    v_prev = next((x for x in reversed(vix[:i]) if x is not None), None)
    volatility = None
    if v_now is not None:
        volatility = {"vix": r(v_now), "band": vix_band(v_now, rules), "chg_1d": r(v_now - v_prev) if v_prev else None,
                      "date": t.date.isoformat()}

    macro = {}
    for k in FRED_SERIES:
        if not isinstance(fetched[k], Exception):
            macro[k] = series_block(fetched[k][1])
            status[k]["last_date"] = macro[k]["date"]
    if not isinstance(fetched["dxy"], Exception):
        db = drop_incomplete(fetched["dxy"][1], now_ny)
        macro["dxy"] = series_block([(b.date, b.close) for b in db], pct=True)
        status["dxy"]["last_date"] = macro["dxy"]["date"]
    if "spread_10y2y" in macro:
        macro["spread_10y2y"]["inverted"] = macro["spread_10y2y"]["value"] < 0
    if "hy_spread" in macro:
        h, cr = macro["hy_spread"], rules["credit"]
        h["warning"] = bool(h["value"] >= cr["level_pct"] or (h.get("chg_20d") or 0) >= cr["change_20d_pp"])

    # 출처 실패 → 직전 값 이어 쓰기
    prev_macro = (prev_latest or {}).get("macro", {})
    for k in list(FRED_SERIES) + ["dxy"]:
        if k not in macro and k in prev_macro and prev_macro[k]:
            macro[k] = {**prev_macro[k], "stale": True}
    if volatility is None and prev_latest and prev_latest.get("volatility"):
        volatility = {**prev_latest["volatility"], "stale": True}

    # 실탄
    am = rules["ammo"]
    basis_dd = t.dd_ath if am.get("basis", "ath") == "ath" else t.dd_cycle
    tiers = [{**tier, "reached": basis_dd <= tier["drawdown_pct"],
              "distance_pp": r(basis_dd - tier["drawdown_pct"]) if basis_dd > tier["drawdown_pct"] else None}
             for tier in am["tiers"]]
    reached = sum(1 for x in tiers if x["reached"])
    ammo = {"basis": am.get("basis", "ath"), "drawdown": r(basis_dd), "tiers": tiers, "reached_count": reached,
            "next": next((x for x in tiers if not x["reached"]), None)}

    # 최근 세션과 변화 목록
    def flags(k):
        d = days[k]
        return {
            "상태": d.state,
            "200일선": "위" if above200[k] else "아래",
            "50일선": "위" if above50[k] else "아래",
            "50·200일선": ("50일선 위" if (sma50[k] or 0) > (sma200[k] or 0) else "50일선 아래") if sma50[k] and sma200[k] else None,
            "VIX 구간": vix_band(vix[k], rules),
            "실탄 단계": sum(1 for tier in am["tiers"] if (d.dd_ath if am.get("basis", "ath") == "ath" else d.dd_cycle) <= tier["drawdown_pct"]),
        }
    changes = []
    lo = max(1, i - 19)
    for k in range(lo, i + 1):
        f0, f1 = flags(k - 1), flags(k)
        for key in f1:
            if f1[key] != f0[key] and f0[key] is not None and f1[key] is not None:
                changes.append({"date": dates[k].isoformat(), "what": key, "from": f0[key], "to": f1[key]})
        if days[k].ath_date == days[k].date and days[k - 1].ath < days[k].close:
            changes.append({"date": dates[k].isoformat(), "what": "사상 최고치", "from": None, "to": r(closes[k])})

    sessions = [{"date": dates[k].isoformat(), "close": r(closes[k]), "chg_pct": r((closes[k] / closes[k - 1] - 1) * 100),
                 "state": days[k].state, "dd_cycle": r(days[k].dd_cycle), "dd_ath": r(days[k].dd_ath), "vix": r(vix[k])}
                for k in range(max(1, i - 9), i + 1)]

    latest = {
        "generated_at_kst": now_utc.astimezone(KST).isoformat(timespec="minutes"),
        "as_of": t.date.isoformat(),
        "state": {"current": t.state, "since": days[j].date.isoformat(), "sessions_in_state": i - j + 1,
                  "previous": prev_state, "reasons": t.alert_reasons},
        "index": index_block, "trend": trend, "momentum": momentum, "volatility": volatility,
        "macro": macro, "ammo": ammo, "sessions": sessions, "recent_changes": changes, "sources": status,
        "rules": {"correction_pct": st["correction_pct"], "bear_pct": st["bear_pct"],
                  "bull_rebound_pct": st["bull_rebound_pct"], "alert": rules["alert"], "vix": rules["vix"],
                  "credit": rules["credit"]},
    }

    h = rules["history"]
    n = h["recent_days"]
    recent = {
        "dates": [d.isoformat() for d in dates[-n:]],
        "close": [r(x) for x in closes[-n:]],
        "sma50": [r(x) for x in sma50[-n:]],
        "sma200": [r(x) for x in sma200[-n:]],
        "dd_ath": [r(d.dd_ath) for d in days[-n:]],
        "dd_cycle": [r(d.dd_cycle) for d in days[-n:]],
        "vix": [r(x) for x in vix[-n:]],
        "state": [d.state for d in days[-n:]],
    }
    ev = stt.events(days, date.fromisoformat(h["events_since"]))
    eps = stt.episodes(days, date.fromisoformat(h["episodes_since"]), am["tiers"])
    return latest, recent, ev, eps


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", type=Path)
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--now", help="ISO 시각 (테스트용)")
    a = ap.parse_args(argv)
    now = datetime.fromisoformat(a.now) if a.now else datetime.now(timezone.utc)
    rules = load_rules()
    prev_path = a.data / "latest.json"
    prev = json.loads(prev_path.read_text(encoding="utf-8")) if prev_path.exists() else None

    fetched = fetch_all(rules, a.offline)
    try:
        latest, recent, ev, eps = compute(fetched, rules, now, prev)
    except src.SourceError as e:
        write_json(a.data / "status.json", {"ok": False, "at_kst": now.astimezone(KST).isoformat(timespec="minutes"),
                                            "error": str(e)})
        print(f"실패: {e}", file=sys.stderr)
        return 1

    write_json(a.data / "latest.json", latest)
    write_json(a.data / "recent.json", recent)
    write_json(a.data / "events.json", ev)
    write_json(a.data / "episodes.json", eps)
    write_json(a.data / "daily" / f"{latest['as_of']}.json", latest)
    failed = [k for k, v in latest["sources"].items() if not v["ok"]]
    write_json(a.data / "status.json", {"ok": True, "at_kst": latest["generated_at_kst"], "as_of": latest["as_of"],
                                        "failed_sources": failed})
    print(f"{latest['as_of']} {latest['state']['current']} 낙폭 {latest['index']['dd_cycle']}% "
          f"(실패 출처: {', '.join(failed) or '없음'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
