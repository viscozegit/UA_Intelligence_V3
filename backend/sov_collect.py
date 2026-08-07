"""
앱 단위 SOV(노출 점유율) 수집 — Sensor Tower network_analysis.

목적: 소재 순위(서수)만으로는 조합 간 절대 규모를 알 수 없다.
앱×네트워크×국가×주 단위 SOV를 수집해 '소재 진입 × 앱 SOV 급증' 동기화를
탐지하면 대박소재의 데이터 증거가 된다.

대상 선정(주간): 최신 주의 must-watch급 앱(신규+상위 5%) + 소재 급증 광고주.
수집 범위: 지원 네트워크 전체 × US·KR × 관측 全주차 (앱당 API 1~2콜).

한계:
- Meta Audience Network는 이 엔드포인트가 지원하지 않음 (SOV 미확인으로 남음)
- unified 출처의 hex app_id는 os별 id로 해석 불가 → 수집 건너뜀 (로그 남김)

사용법: python sov_collect.py [--apps app_id1,app_id2] [--weeks 26]
"""
import argparse
import asyncio
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("SENSORTOWER_API_TOKEN", "")
DB_PATH = BASE_DIR / "snapshots.db"

# network_analysis가 지원하는 네트워크만 (Meta Audience Network 미지원)
NA_NETWORKS = ["Applovin", "Unity", "Admob", "Vungle", "Youtube", "TikTok", "Instagram"]
COUNTRIES = "US,KR"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_sov (
    app_id     TEXT NOT NULL,
    network    TEXT NOT NULL,
    country    TEXT NOT NULL,
    week       TEXT NOT NULL,     -- 주 시작일(월요일)
    sov        REAL,              -- 노출 점유율 (0~1)
    fetched_at TEXT,
    PRIMARY KEY (app_id, network, country, week)
);
CREATE INDEX IF NOT EXISTS idx_sov_app ON app_sov(app_id, week);
"""


def guess_os(app_id: str):
    """app_id 형태로 스토어 추정. hex 24자리(unified)는 해석 불가."""
    if re.fullmatch(r"\d+", app_id):
        return "ios"
    if re.fullmatch(r"[0-9a-f]{24}", app_id):
        return None          # unified id — os별 엔드포인트로 조회 불가
    return "android"


def pick_target_apps(conn, latest_week: str) -> list[str]:
    """최신 주 기준 관심 앱: 신규+상위 5% 소재 보유 앱 ∪ 소재 급증(+8) 앱"""
    hot = [r[0] for r in conn.execute("""
        WITH c AS (SELECT week,platform,network,country,ad_type,COUNT(*) n
                   FROM weekly_snapshots GROUP BY 1,2,3,4,5),
        s AS (SELECT w.*, (w.rank*100.0/c.n) pct FROM weekly_snapshots w
              JOIN c USING(week,platform,network,country,ad_type)
              WHERE c.n>=20 AND w.unit_id!='')
        SELECT DISTINCT app_id FROM s
        WHERE week=? AND pct<=5 AND unit_id IN (
          SELECT unit_id FROM s GROUP BY unit_id HAVING MIN(week)=?)""",
        (latest_week, latest_week))]

    prev_week = (date.fromisoformat(latest_week) - timedelta(days=7)).isoformat()
    movers = [r[0] for r in conn.execute("""
        SELECT app_id FROM (
          SELECT app_id,
            COUNT(DISTINCT CASE WHEN week=? THEN unit_id END) cur,
            COUNT(DISTINCT CASE WHEN week=? THEN unit_id END) prv
          FROM weekly_snapshots WHERE week IN (?,?) AND unit_id!='' GROUP BY app_id)
        WHERE prv>=3 AND cur-prv>=8""",
        (latest_week, prev_week, latest_week, prev_week))]

    out = []
    for a in hot + movers:
        if a and a not in out:
            out.append(a)
    return out[:20]


async def fetch_app(client, conn, app_id: str, start: str, end: str):
    os_ = guess_os(app_id)
    if os_ is None:
        print(f"  ⏭  {app_id}: unified id — os 해석 불가, 건너뜀")
        return 0
    try:
        r = await client.get(
            f"https://api.sensortower.com/v1/{os_}/ad_intel/network_analysis",
            params={"auth_token": TOKEN, "app_ids": app_id,
                    "start_date": start, "end_date": end,
                    "countries": COUNTRIES, "networks": ",".join(NA_NETWORKS),
                    "period": "week"})
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        print(f"  ❌ {app_id}: HTTP {status}")
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO app_sov VALUES (?,?,?,?,?,?)",
            (str(row["app_id"]), row["network"], row["country"],
             row["date"], row.get("sov"), now))
    conn.commit()
    print(f"  ✓ {app_id} ({os_}): {len(rows)}행")
    return len(rows)


async def main(app_ids, n_weeks):
    if not TOKEN:
        sys.exit("SENSORTOWER_API_TOKEN 없음 (backend/.env)")
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    latest = conn.execute("SELECT MAX(week) FROM weekly_snapshots").fetchone()[0]
    start = (date.fromisoformat(latest) - timedelta(weeks=n_weeks)).isoformat()

    targets = app_ids or pick_target_apps(conn, latest)
    print(f"대상 앱 {len(targets)}개 / 기간 {start} ~ {latest}")

    total = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for a in targets:
            total += await fetch_app(client, conn, a, start, latest)
            await asyncio.sleep(1)
    n = conn.execute("SELECT COUNT(*) FROM app_sov").fetchone()[0]
    print(f"\n수집 {total}행 / app_sov 누적 {n:,}행")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", type=str, default="")
    ap.add_argument("--weeks", type=int, default=26)
    args = ap.parse_args()
    ids = [a.strip() for a in args.apps.split(",") if a.strip()]
    asyncio.run(main(ids, args.weeks))
