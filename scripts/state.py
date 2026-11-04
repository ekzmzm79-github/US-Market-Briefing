"""시장 상태 판정. 전체 이력을 처음부터 다시 훑어 매일의 상태를 정한다.

저장해 둔 "어제 상태"에 기대지 않으므로, 하루 계산을 빠뜨리거나 규칙을 바꿔도 결과가 꼬이지 않는다.

- 약세장은 기억을 가진다: -20%로 들어가면 저점 대비 +20% 반등해야 빠진다.
- 약세장이 끝나면 새 사이클이 시작되고, 이후 조정·약세 낙폭은 새 사이클의 고점 기준으로 잰다
  (2023년 가을 조정처럼, 사상 최고치 아래에서도 조정장이 정의되는 관례를 따른다).
- 조정장은 여유폭을 두고 빠진다(correction_exit_buffer_pct).
- 경계는 조건이 모두 풀린 날이 N일 연속이어야 해제된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

NORMAL, ALERT, CORRECTION, BEAR = "정상", "경계", "조정장", "약세장"
ORDER = {NORMAL: 0, ALERT: 1, CORRECTION: 2, BEAR: 3}


@dataclass
class Day:
    date: date
    close: float
    state: str
    ath: float
    ath_date: date
    dd_ath: float            # 사상 최고 종가 대비 %
    cycle_peak: float
    cycle_peak_date: date
    dd_cycle: float          # 사이클 고점 대비 %
    trough: float | None = None        # 약세장일 때 지금까지의 저점
    trough_date: date | None = None
    alert_reasons: list[str] = field(default_factory=list)


def classify(dates, closes, sma50, sma200, vix, rules) -> list[Day]:
    st, al = rules["state"], rules["alert"]
    corr, bear = st["correction_pct"], st["bear_pct"]
    corr_exit = corr + st.get("correction_exit_buffer_pct", 0)
    rebound = st["bull_rebound_pct"]

    out: list[Day] = []
    ath = -1.0
    ath_d = dates[0]
    cyc_peak, cyc_peak_d = closes[0], dates[0]
    in_bear = in_corr = in_alert = False
    trough = trough_d = None
    trough_i = 0
    below200 = calm = 0

    for i, (d, c) in enumerate(zip(dates, closes)):
        if c > ath:
            ath, ath_d = c, d
        if c >= cyc_peak:
            cyc_peak, cyc_peak_d = c, d
        dd_c = round((c / cyc_peak - 1) * 100, 6)  # 경계값(-10.000…)이 부동소수 오차로 빗나가지 않게

        # 약세장 진입·유지·해제
        if in_bear:
            if c < trough:
                trough, trough_d, trough_i = c, d, i
            if c >= trough * (1 + rebound / 100):
                in_bear = in_corr = False
                # 새 사이클: 저점 이후 최고 종가를 고점으로
                j = max(range(trough_i, i + 1), key=lambda k: closes[k])
                cyc_peak, cyc_peak_d = closes[j], dates[j]
                dd_c = round((c / cyc_peak - 1) * 100, 6)
                trough = trough_d = None
        elif dd_c <= bear:
            in_bear, trough, trough_d, trough_i = True, c, d, i

        # 조정장
        if not in_bear:
            if dd_c <= corr:
                in_corr = True
            elif in_corr and dd_c > corr_exit:
                in_corr = False

        # 경계 조건
        s50, s200, v = sma50[i], sma200[i], vix[i]
        below200 = below200 + 1 if (s200 is not None and c < s200) else 0
        reasons = []
        if below200 >= al["below_sma200_days"]:
            reasons.append(f"200일선 아래 {below200}거래일째")
        if v is not None and v >= al["vix_level"]:
            reasons.append(f"VIX {v:.1f} (기준 {al['vix_level']} 이상)")
        if al.get("sma50_below_sma200") and s50 is not None and s200 is not None and s50 < s200:
            reasons.append("50일선이 200일선 아래 (데드크로스 상태)")
        if reasons:
            in_alert, calm = True, 0
        else:
            calm += 1
            if in_alert and calm >= al["exit_calm_days"]:
                in_alert = False
            if in_alert:
                reasons = [f"해제 대기: 조건 해소 {calm}/{al['exit_calm_days']}일"]

        state = BEAR if in_bear else CORRECTION if in_corr else ALERT if in_alert else NORMAL
        out.append(Day(d, c, state, ath, ath_d, (c / ath - 1) * 100, cyc_peak, cyc_peak_d, dd_c,
                       trough, trough_d, reasons if state == ALERT else []))
    return out


def events(days: list[Day], since: date) -> list[dict]:
    """상태가 바뀐 날 목록."""
    ev = []
    for prev, cur in zip(days, days[1:]):
        if cur.state != prev.state and cur.date >= since:
            ev.append({
                "date": cur.date.isoformat(),
                "from": prev.state,
                "to": cur.state,
                "direction": "악화" if ORDER[cur.state] > ORDER[prev.state] else "완화",
                "close": round(cur.close, 2),
                "dd_cycle": round(cur.dd_cycle, 2),
                "dd_ath": round(cur.dd_ath, 2),
            })
    return ev


def episodes(days: list[Day], since: date, ammo_tiers: list[dict]) -> list[dict]:
    """조정장·약세장이 이어진 구간마다 고점, 저점, 깊이."""
    out, seg = [], []

    def flush():
        if not seg:
            return
        low = min(seg, key=lambda x: x.close)
        first = seg[0]
        if first.date < since and low.date < since:
            return
        depth = (low.close / first.cycle_peak - 1) * 100
        out.append({
            "peak_date": first.cycle_peak_date.isoformat(),
            "trough_date": low.date.isoformat(),
            "end_date": seg[-1].date.isoformat(),
            "ongoing": seg[-1] is days[-1],
            "depth_cycle": round(depth, 2),
            "dd_ath_at_trough": round(low.dd_ath, 2),
            "reached_bear": any(x.state == BEAR for x in seg),
            "ammo_tiers_hit": sum(1 for t in ammo_tiers if low.dd_ath <= t["drawdown_pct"]),
        })

    for d in days:
        if d.state in (CORRECTION, BEAR):
            seg.append(d)
        else:
            flush()
            seg = []
    flush()
    return out
