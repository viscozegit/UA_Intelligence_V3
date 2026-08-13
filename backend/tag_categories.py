"""
weekly_snapshots에 등장한 모든 앱의 실제 등록 카테고리를 조회해 태깅한다.

배경: backfill.py는 항상 category='6014'/'game'(Games 전체)로만 수집했기 때문에
weekly_snapshots에는 장르 정보가 없다. 장르별로 다시 수집하면 API 호출이 4배로
늘고 과거 26주에는 소급 적용도 안 된다. 대신 이미 모은 앱들의 '진짜 등록 장르'를
Sensor Tower 앱 상세 API로 역조회해 태깅하면, 기존 데이터 전체에 즉시 적용된다.

app_id 유형 3가지:
- 숫자 (iOS 고유 id) → /v1/ios/apps
- 24자리 hex (unified id, Meta 등) → /v1/unified/apps 로 ios/android id 변환 후 재조회
- 그 외 (Android 패키지명) → /v1/android/apps

사용법: python tag_categories.py
"""
import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("SENSORTOWER_API_TOKEN", "")
DB_PATH = BASE_DIR / "snapshots.db"
BATCH = 100

# 우리가 관심 있는 카테고리만 매핑 (그 외는 other로 태깅)
IOS_CAT_LABEL = {7001: "action", 7014: "role_playing", 7017: "strategy"}
AND_CAT_LABEL = {"game_action": "action", "game_role_playing": "role_playing", "game_strategy": "strategy"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_categories (
    app_id     TEXT PRIMARY KEY,   -- weekly_snapshots.app_id 그대로 (hex 포함)
    genres     TEXT,               -- 콤마구분: action,role_playing,strategy 등 매칭된 것만
    fetched_at TEXT
);
"""


def classify(app_id: str) -> str:
    if re.fullmatch(r"\d+", app_id):
        return "ios"
    if re.fullmatch(r"[0-9a-f]{24}", app_id):
        return "unified"
    return "android"


async def fetch_batch(client, os_, ids):
    r = await client.get(f"https://api.sensortower.com/v1/{os_}/apps",
                          params={"auth_token": TOKEN, "app_ids": ",".join(ids)})
    r.raise_for_status()
    return r.json().get("apps", [])


async def resolve_unified(client, ids):
    """hex unified id → (ios_id, android_id) 매핑"""
    r = await client.get("https://api.sensortower.com/v1/unified/apps",
                          params={"auth_token": TOKEN, "app_ids": ",".join(ids), "app_id_type": "unified"})
    r.raise_for_status()
    out = {}
    for a in r.json().get("apps", []):
        uid = a.get("unified_app_id")
        ios_id = str(a["itunes_apps"][0]["app_id"]) if a.get("itunes_apps") else None
        and_id = a["android_apps"][0]["app_id"] if a.get("android_apps") else None
        out[uid] = (ios_id, and_id)
    return out


def genres_from(app: dict, label_map: dict) -> str:
    cats = app.get("categories") or []
    hits = [label_map[c] for c in cats if c in label_map]
    return ",".join(sorted(set(hits)))


async def main():
    if not TOKEN:
        sys.exit("SENSORTOWER_API_TOKEN 없음")
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    all_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT app_id FROM weekly_snapshots WHERE app_id != ''")]
    done = {r[0] for r in conn.execute("SELECT app_id FROM app_categories")}
    todo = [a for a in all_ids if a not in done]
    print(f"전체 {len(all_ids):,}개 / 이미 태깅 {len(done):,}개 / 신규 {len(todo):,}개")

    ios_ids = [a for a in todo if classify(a) == "ios"]
    and_ids = [a for a in todo if classify(a) == "android"]
    hex_ids = [a for a in todo if classify(a) == "unified"]

    now = datetime.now().isoformat(timespec="seconds")
    rows = []

    async with httpx.AsyncClient(timeout=30) as client:
        # ① iOS 숫자 id
        for i in range(0, len(ios_ids), BATCH):
            chunk = ios_ids[i:i + BATCH]
            try:
                apps = await fetch_batch(client, "ios", chunk)
                found = {str(a["app_id"]): a for a in apps}
                for aid in chunk:
                    a = found.get(aid, {})
                    rows.append((aid, genres_from(a, IOS_CAT_LABEL), now))
            except Exception as e:
                print(f"  ❌ ios batch {i}: {e}")
            print(f"  ios {min(i+BATCH,len(ios_ids))}/{len(ios_ids)}")

        # ② Android 패키지명
        for i in range(0, len(and_ids), BATCH):
            chunk = and_ids[i:i + BATCH]
            try:
                apps = await fetch_batch(client, "android", chunk)
                found = {a["app_id"]: a for a in apps}
                for aid in chunk:
                    a = found.get(aid, {})
                    rows.append((aid, genres_from(a, AND_CAT_LABEL), now))
            except Exception as e:
                print(f"  ❌ android batch {i}: {e}")
            print(f"  android {min(i+BATCH,len(and_ids))}/{len(and_ids)}")

        # ③ unified hex id → ios/android로 변환 후 조회
        resolved = {}
        for i in range(0, len(hex_ids), BATCH):
            chunk = hex_ids[i:i + BATCH]
            try:
                resolved.update(await resolve_unified(client, chunk))
            except Exception as e:
                print(f"  ❌ unified resolve {i}: {e}")
            print(f"  unified resolve {min(i+BATCH,len(hex_ids))}/{len(hex_ids)}")

        need_ios = {v[0] for v in resolved.values() if v[0]}
        need_and = {v[1] for v in resolved.values() if v[1]}
        ios_cache, and_cache = {}, {}
        need_ios_l, need_and_l = list(need_ios), list(need_and)
        for i in range(0, len(need_ios_l), BATCH):
            chunk = need_ios_l[i:i + BATCH]
            try:
                for a in await fetch_batch(client, "ios", chunk):
                    ios_cache[str(a["app_id"])] = a
            except Exception as e:
                print(f"  ❌ ios(resolved) batch: {e}")
        for i in range(0, len(need_and_l), BATCH):
            chunk = need_and_l[i:i + BATCH]
            try:
                for a in await fetch_batch(client, "android", chunk):
                    and_cache[a["app_id"]] = a
            except Exception as e:
                print(f"  ❌ android(resolved) batch: {e}")

        for hid in hex_ids:
            ios_id, and_id = resolved.get(hid, (None, None))
            genres = set()
            if ios_id and ios_id in ios_cache:
                genres.update(genres_from(ios_cache[ios_id], IOS_CAT_LABEL).split(","))
            if and_id and and_id in and_cache:
                genres.update(genres_from(and_cache[and_id], AND_CAT_LABEL).split(","))
            genres.discard("")
            rows.append((hid, ",".join(sorted(genres)), now))

    conn.executemany("INSERT OR REPLACE INTO app_categories VALUES (?,?,?)", rows)
    conn.commit()

    tagged = conn.execute("SELECT COUNT(*) FROM app_categories WHERE genres != ''").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM app_categories").fetchone()[0]
    print(f"\n완료: {total:,}개 태깅 시도 / {tagged:,}개 액션·롤플레잉·전략 매칭")


if __name__ == "__main__":
    asyncio.run(main())
