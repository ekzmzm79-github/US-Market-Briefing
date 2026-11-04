import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import compute  # noqa: E402
import indicators as ind  # noqa: E402
import sources as src  # noqa: E402
import state as stt  # noqa: E402

RULES = compute.load_rules()


def days_from(closes, vix=None, s50=None, s200=None):
    n = len(closes)
    dates = [date(2020, 1, 1) + timedelta(days=k) for k in range(n)]
    return stt.classify(dates, closes, s50 or [None] * n, s200 or [None] * n, vix or [None] * n, RULES)


# ---------- 상태 판정 ----------

def test_bear_needs_20pct_rebound_from_trough():
    closes = [100, 95, 85, 79, 75, 80, 85, 89, 90, 91]
    st = [d.state for d in days_from(closes)]
    assert st[:2] == ["정상", "정상"]
    assert st[2] == "조정장"           # -15%
    assert st[3] == "약세장"           # -21%
    assert st[5:8] == ["약세장"] * 3   # 낙폭은 -20% 안이지만 저점(75) 대비 +20%(90) 전
    assert st[8] == "정상"             # 90 = 75 × 1.2 → 새 사이클
    d = days_from(closes)[8]
    assert d.cycle_peak == 90 and d.dd_ath == pytest.approx(-10)


def test_new_cycle_peak_used_after_bear():
    # 약세장 해제 뒤 새 고점(100) 기준 -10%면 사상 최고치 아래여도 조정장
    closes = [200, 150, 100, 120, 130, 116]
    days = days_from(closes)
    assert days[3].state == "정상"
    assert days[5].state == "조정장"
    assert days[5].dd_cycle == pytest.approx(-10.77, abs=0.01)
    assert days[5].dd_ath == pytest.approx(-42)


def test_correction_exit_buffer():
    st = [d.state for d in days_from([100, 89, 91, 93])]
    assert st == ["정상", "조정장", "조정장", "정상"]  # -9%는 여유폭(-8%) 안이라 유지


def test_alert_sma200_streak_and_exit():
    closes = [100] * 10
    s200 = [101, 101, 101, 99, 99, 99, 99, 99, 99, 99]  # 0~2일 아래, 3일째부터 위
    days = days_from(closes, s200=s200)
    st = [d.state for d in days]
    assert st[:2] == ["정상", "정상"]
    assert st[2] == "경계"            # 3거래일째 아래
    assert st[3:5] == ["경계", "경계"]  # 해제 대기 1, 2일
    assert st[5] == "정상"            # 3일 연속 조건 해소
    assert "200일선 아래 3거래일째" in days[2].alert_reasons


def test_alert_vix_and_deadcross():
    days = days_from([100, 100], vix=[24.9, 25.0])
    assert [d.state for d in days] == ["정상", "경계"]
    days = days_from([100], s50=[90], s200=[95])
    assert days[0].state == "경계" and "데드크로스" in days[0].alert_reasons[0]


def test_events_and_episodes():
    days = days_from([100, 95, 85, 79, 75, 90, 91])
    ev = stt.events(days, date(2000, 1, 1))
    assert [(e["from"], e["to"]) for e in ev] == [("정상", "조정장"), ("조정장", "약세장"), ("약세장", "정상")]
    eps = stt.episodes(days, date(2000, 1, 1), RULES["ammo"]["tiers"])
    assert len(eps) == 1 and eps[0]["depth_cycle"] == -25 and eps[0]["reached_bear"] and eps[0]["ammo_tiers_hit"] == 1


# ---------- 지표 ----------

def test_sma_ema():
    assert ind.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ind.ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2 and e[3] == pytest.approx(3) and e[4] == pytest.approx(4)


def test_rsi_extremes():
    assert ind.rsi(list(range(1, 30)))[-1] == 100
    assert ind.rsi(list(range(30, 1, -1)))[-1] == pytest.approx(0)
    assert ind.rsi(list(range(10)))[9] is None  # 데이터 부족


def test_adx_strong_trend_and_length():
    n = 80
    close = [100 + k for k in range(n)]
    a = ind.adx([c + 1 for c in close], [c - 1 for c in close], close)
    assert len(a) == n and a[26] is None and a[27] is not None
    assert a[-1] > 90


def test_streak():
    assert ind.streak([True, True, False, True]) == [1, 2, 0, 1]


# ---------- 출처 응답 해석 ----------

def yahoo_json(dates, closes):
    ts = [int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc).timestamp()) for d in dates]
    return json.dumps({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{
        "open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": [0] * len(closes)}]}}]}})


def test_parse_yahoo_skips_null_close():
    text = yahoo_json([date(2026, 1, 2), date(2026, 1, 5)], [10.0, 11.0])
    data = json.loads(text)
    data["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    bars = src.parse_yahoo(json.dumps(data))
    assert [b.date for b in bars] == [date(2026, 1, 2)]


def test_parse_yahoo_bad():
    with pytest.raises(src.SourceError):
        src.parse_yahoo('{"chart": {"result": null}}')


def test_parse_fred_skips_missing():
    pts = src.parse_fred("observation_date,DGS10\n2026-01-01,.\n2026-01-02,4.1\n2026-01-05,\n", "DGS10")
    assert pts == [(date(2026, 1, 2), 4.1)]
    pts = src.parse_fred("DATE,DGS10\n2026-01-02,4.1\n", "DGS10")  # 예전 머리글
    assert pts == [(date(2026, 1, 2), 4.1)]


def test_parse_cboe_and_stooq():
    assert src.parse_cboe_vix("DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2026,15,16,14,15.5\n")[0].close == 15.5
    assert src.parse_stooq("Date,Open,High,Low,Close,Volume\n2026-01-02,1,2,0.5,1.5,0\n")[0].close == 1.5


def test_drop_incomplete_bar():
    bars = [src.Bar(date(2026, 11, 3), 1, 1, 1, 1), src.Bar(date(2026, 11, 4), 1, 1, 1, 1)]
    ny = src.NY
    assert len(compute.drop_incomplete(bars, datetime(2026, 11, 4, 12, 0, tzinfo=ny))) == 1
    assert len(compute.drop_incomplete(bars, datetime(2026, 11, 4, 17, 0, tzinfo=ny))) == 2


# ---------- 전체 흐름 ----------

def make_offline(tmp: Path, n=700, crash_at=None):
    start = date(2023, 1, 2)
    dates, k = [], 0
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    closes = []
    for k in range(n):
        c = 4000 * (1 + 0.0004 * k) * (1 + 0.02 * math.sin(k / 15))
        if crash_at and k >= crash_at:
            c *= 0.85
        closes.append(round(c, 2))
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "gspc.json").write_text(yahoo_json(dates, closes))
    (tmp / "vix.json").write_text(yahoo_json(dates, [18.0] * (n - 1) + [27.0]))
    (tmp / "dxy.json").write_text(yahoo_json(dates, [100.0 + k * 0.01 for k in range(n)]))
    for s, base in [("DGS10", 4.0), ("DGS2", 3.5), ("T10Y2Y", 0.5), ("BAMLH0A0HYM2", 3.0)]:
        rows = "\n".join(f"{dd.isoformat()},{base + 0.001 * i:.3f}" for i, dd in enumerate(dates))
        (tmp / f"{s}.csv").write_text(f"observation_date,{s}\n{rows}\n")
    return dates


def test_end_to_end_offline(tmp_path):
    dates = make_offline(tmp_path / "raw", crash_at=690)
    out = tmp_path / "data"
    now = datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time(), timezone.utc).replace(hour=22).isoformat()
    assert compute.main(["--offline", str(tmp_path / "raw"), "--data", str(out), "--now", now]) == 0
    latest = json.loads((out / "latest.json").read_text())
    assert latest["as_of"] == dates[-1].isoformat()
    assert latest["state"]["current"] in ("조정장", "경계", "약세장")
    assert latest["volatility"]["band"] == "보통" and latest["volatility"]["vix"] == 27
    assert latest["macro"]["hy_spread"]["warning"] is False
    assert latest["ammo"]["tiers"][0]["drawdown_pct"] == -20
    assert any(c["what"] == "상태" for c in latest["recent_changes"])
    assert len(json.loads((out / "recent.json").read_text())["close"]) == 260
    assert (out / "daily" / f"{dates[-1].isoformat()}.json").exists()
    assert json.loads((out / "status.json").read_text())["ok"] is True


def test_missing_source_carries_previous_value(tmp_path):
    dates = make_offline(tmp_path / "raw")
    out = tmp_path / "data"
    now = datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time(), timezone.utc).replace(hour=22).isoformat()
    assert compute.main(["--offline", str(tmp_path / "raw"), "--data", str(out), "--now", now]) == 0
    (tmp_path / "raw" / "BAMLH0A0HYM2.csv").unlink()
    assert compute.main(["--offline", str(tmp_path / "raw"), "--data", str(out), "--now", now]) == 0
    latest = json.loads((out / "latest.json").read_text())
    assert latest["macro"]["hy_spread"]["stale"] is True
    assert latest["sources"]["hy_spread"]["ok"] is False


def test_index_failure_keeps_files(tmp_path):
    make_offline(tmp_path / "raw")
    out = tmp_path / "data"
    assert compute.main(["--offline", str(tmp_path / "raw"), "--data", str(out)]) == 0
    before = (out / "latest.json").read_text()
    (tmp_path / "raw" / "gspc.json").unlink()
    assert compute.main(["--offline", str(tmp_path / "raw"), "--data", str(out)]) == 1
    assert (out / "latest.json").read_text() == before
    assert json.loads((out / "status.json").read_text())["ok"] is False
