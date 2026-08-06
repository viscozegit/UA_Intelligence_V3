"""
Sensor Tower 주별 상위 소재 스냅샷 백필 스크립트.

사용법:
    python backfill.py                # 기본: 최근 26주
    python backfill.py --weeks 52    # 주 수 변경

- 완료된 (주, 조합)은 fetch_log에 기록되어 재실행 시 건너뜀 (중단 후 이어받기 가능)
- 429(rate limit) 발생 시 지수 백오프로 재시도
- 저장: backend/snapshots.db (SQLite)
"""
import argparse
import asyncio
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

sys.path.insert(0, str(BASE_DIR))
from sensortower import SensorTowerClient, UNIFIED_ONLY_NETWORKS  # noqa: E402

DB_PATH = BASE_DIR / "snapshots.db"

# ── 수집 범위 ────────────────────────────────────────────────
# 우선순위 순 — Meta·TikTok(unified, 쿼터 제한)이 먼저 쿼터를 쓰도록 앞에 배치
NETWORKS = [
    "Meta Audience Network", "TikTok",
    "Youtube", "Applovin", "Unity", "Admob", "Vungle",
]
COUNTRIES = ["US", "KR"]
AD_TYPES = ["video", "image"]
PLATFORMS = ["ios", "android"]
LIMIT = 100
CONCURRENCY = 4
MAX_RETRIES = 4


# ── DB ──────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_snapshots (
    week           TEXT NOT NULL,   -- 주 시작일 (월요일)
    platform       TEXT NOT NULL,
    network        TEXT NOT NULL,
    country        TEXT NOT NULL,
    ad_type        TEXT NOT NULL,
    rank           INTEGER NOT NULL,
    unit_id        TEXT,
    app_id         TEXT,
    app_name       TEXT,
    publisher      TEXT,
    icon_url       TEXT,
    first_seen_at  TEXT,
    last_seen_at   TEXT,
    phashion_group TEXT,
    creative_id    TEXT,
    creative_url   TEXT,
    preview_url    TEXT,
    thumb_url      TEXT,
    width          INTEGER,
    height         INTEGER,
    video_duration REAL,
    message        TEXT,
    fetched_at     TEXT,
    PRIMARY KEY (week, platform, network, country, ad_type, rank)
);

CREATE INDEX IF NOT EXISTS idx_snap_app     ON weekly_snapshots(app_id);
CREATE INDEX IF NOT EXISTS idx_snap_unit    ON weekly_snapshots(unit_id);
CREATE INDEX IF NOT EXISTS idx_snap_phash   ON weekly_snapshots(phashion_group);

CREATE TABLE IF NOT EXISTS fetch_log (
    week     TEXT NOT NULL,
    platform TEXT NOT NULL,
    network  TEXT NOT NULL,
    country  TEXT NOT NULL,
    ad_type  TEXT NOT NULL,
    status   TEXT NOT NULL,   -- ok | error
    n_units  INTEGER,
    error    TEXT,
    fetched_at TEXT,
    PRIMARY KEY (week, platform, network, country, ad_type)
);
"""


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def done_combos(conn) -> set:
    rows = conn.execute("SELECT week, platform, network, country, ad_type FROM fetch_log WHERE status='ok'")
    return {tuple(r) for r in rows}


def save_units(conn, combo, units):
    week, platform, network, country, ad_type = combo
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for i, u in enumerate(units):
        info = u.get("app_info") or {}
        cr = (u.get("creatives") or [{}])[0]
        rows.append((
            week, platform, network, country, ad_type, i + 1,
            str(u.get("id") or ""), str(u.get("app_id") or ""),
            info.get("humanized_name") or info.get("name") or "",
            info.get("publisher_name") or "",
            info.get("icon_url") or "",
            u.get("first_seen_at"), u.get("last_seen_at"),
            str(u.get("phashion_group") or ""),
            str(cr.get("id") or ""), cr.get("creative_url"), cr.get("preview_url"), cr.get("thumb_url"),
            cr.get("width"), cr.get("height"), cr.get("video_duration"), cr.get("message"),
            now,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO weekly_snapshots VALUES (" + ",".join("?" * 23) + ")", rows
    )
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?,?,?,?,?)",
        (*combo, "ok", len(units), None, now),
    )
    conn.commit()


def log_error(conn, combo, err):
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?,?,?,?,?)",
        (*combo, "error", 0, str(err)[:300], datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


# ── Fetch ───────────────────────────────────────────────────

async def fetch_one(client, sem, conn, combo, progress, pace=0.0):
    week, platform, network, country, ad_type = combo
    async with sem:
        if pace and network in UNIFIED_ONLY_NETWORKS:
            await asyncio.sleep(pace)  # unified 쿼터 보호용 호출 간격 (public은 불필요)
        for attempt in range(MAX_RETRIES):
            try:
                if network in UNIFIED_ONLY_NETWORKS:
                    # TikTok·Meta 등은 unified 엔드포인트만 데이터 반환 (주 단위)
                    data = await client.get_top_creatives_unified(
                        platform=platform, ad_types=ad_type, network=network,
                        country=country, category="6014",
                        date_str=week, limit=LIMIT, period="week",
                    )
                else:
                    data = await client.get_top_creatives(
                        platform=platform, ad_types=ad_type, network=network,
                        country=country,
                        category="game" if platform == "android" else "6014",
                        date_str=week, limit=LIMIT,
                    )
                units = data.get("ad_units", [])
                save_units(conn, combo, units)
                progress["ok"] += 1
                print(f"[{progress['ok'] + progress['err']}/{progress['total']}] {week} {platform}/{network}/{country}/{ad_type}: {len(units)}건")
                return
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 429 and attempt < MAX_RETRIES - 1:
                    wait = 2 ** (attempt + 2)  # 4, 8, 16초
                    print(f"  ⏳ 429 rate limit — {wait}초 대기 후 재시도 ({week} {network}/{country})")
                    await asyncio.sleep(wait)
                    continue
                log_error(conn, combo, e)
                progress["err"] += 1
                print(f"  ❌ {week} {platform}/{network}/{country}/{ad_type}: {str(e)[:100]}")
                return


def mondays(n_weeks: int) -> list[str]:
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    # 진행 중인 이번 주는 제외하고 완결된 주부터
    return [(this_monday - timedelta(weeks=i + 1)).isoformat() for i in range(n_weeks)]


async def main(n_weeks: int, concurrency: int = CONCURRENCY, pace: float = 0.0):
    conn = open_db()
    client = SensorTowerClient()
    if not client.token:
        sys.exit("SENSORTOWER_API_TOKEN이 없습니다 (backend/.env 확인)")

    weeks = mondays(n_weeks)
    # 네트워크를 최외곽에 두어 우선순위 높은 매체(Meta 등)가 먼저 수집되도록 한다
    combos = [
        (w, p, n, c, a)
        for n in NETWORKS for w in weeks for p in PLATFORMS
        for c in COUNTRIES for a in AD_TYPES
    ]
    done = done_combos(conn)
    todo = [c for c in combos if c not in done]

    print(f"전체 {len(combos)}콜 중 완료 {len(combos) - len(todo)}, 남은 {len(todo)}콜")
    print(f"기간: {weeks[-1]} ~ {weeks[0]}")
    if not todo:
        print("이미 전부 완료됨.")
        return

    sem = asyncio.Semaphore(concurrency)
    progress = {"ok": 0, "err": 0, "total": len(todo)}
    await asyncio.gather(*[fetch_one(client, sem, conn, c, progress, pace) for c in todo])

    print(f"\n완료: 성공 {progress['ok']}, 실패 {progress['err']}")
    n = conn.execute("SELECT COUNT(*), COUNT(DISTINCT unit_id) FROM weekly_snapshots").fetchone()
    print(f"DB 누적: {n[0]}행 / 고유 소재 {n[1]}개 → {DB_PATH}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=26)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--pace", type=float, default=0.0, help="호출 간 대기(초) — unified 쿼터 보호")
    args = ap.parse_args()
    asyncio.run(main(args.weeks, args.concurrency, args.pace))
