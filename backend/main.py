from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import asyncio
import os
import sqlite3
import httpx
import urllib.parse
from datetime import date, timedelta

# 실행 위치(cwd)와 무관하게 backend/.env 를 읽는다
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sensortower import (
    SensorTowerClient,
    IOS_NETWORKS, ANDROID_NETWORKS,
    IOS_CATEGORIES, ANDROID_CATEGORIES,
    AD_TYPES, UNIFIED_ONLY_NETWORKS,
)
app = FastAPI(title="UA Intelligence")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = SensorTowerClient()

COUNTRIES = ["US", "KR", "JP", "GB", "DE", "FR", "BR"]


# ── Helpers ─────────────────────────────────────────────────

def get_networks(platform: str) -> list[str]:
    return ANDROID_NETWORKS if platform == "android" else IOS_NETWORKS

def get_categories(platform: str) -> dict:
    return ANDROID_CATEGORIES if platform == "android" else IOS_CATEGORIES

def merge_ad_units(all_units: list[list]) -> list:
    """여러 호출 결과의 ad_units를 unit id 기준으로 중복 제거 후 병합"""
    seen = {}
    for units in all_units:
        for unit in units:
            uid = unit.get("id") or f"{unit['app_id']}_{unit.get('ad_type')}"
            if uid not in seen:
                seen[uid] = unit
    return list(seen.values())

async def fetch_safe(platform, ad_types, network, country, category, date_str, limit):
    """호출 1건 결과. 에러도 삼키지 않고 어떤 조합이 왜 실패했는지 반환.
    (429 쿼터 초과가 '데이터 없음'으로 보이는 문제 방지)"""
    try:
        if network in UNIFIED_ONLY_NETWORKS:
            # Meta Audience Network, TikTok 등은 unified 엔드포인트 사용
            data = await client.get_top_creatives_unified(
                platform=platform, ad_types=ad_types, network=network,
                country=country, category=category, date_str=date_str, limit=limit,
            )
        else:
            data = await client.get_top_creatives(
                platform=platform, ad_types=ad_types, network=network,
                country=country, category=category, date_str=date_str, limit=limit,
            )
        units = data.get("ad_units", [])
        for u in units:
            u["network"] = network
            u["country"] = country
            u["platform"] = platform
        return {"units": units, "error": None}
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        reason = f"HTTP {status}" if status else type(e).__name__
        return {"units": [], "error": f"{network}/{country}/{platform}/{ad_types}: {reason}"}


# ── API Routes ──────────────────────────────────────────────



@app.get("/api/meta")
async def meta(platform: str = Query("ios")):
    """플랫폼별 유효한 네트워크·카테고리·ad_types 반환"""
    if platform == "all":
        # 우선순위 순서 유지하며 병합 (정렬하면 매체 우선순위가 깨짐)
        networks = IOS_NETWORKS + [n for n in ANDROID_NETWORKS if n not in IOS_NETWORKS]
        categories = IOS_CATEGORIES   # label은 동일
    elif platform == "android":
        networks = ANDROID_NETWORKS
        categories = ANDROID_CATEGORIES
    else:
        networks = IOS_NETWORKS
        categories = IOS_CATEGORIES

    return {
        "networks": networks,
        "categories": categories,
        "ad_types": AD_TYPES,
        "countries": COUNTRIES,
    }


@app.get("/api/top-advertisers")
async def top_advertisers(
    platform: str = Query("ios"),       # "ios" | "android" | "all"
    ad_types: str = Query("video"),     # 단일값 또는 "all"
    network: str = Query("Applovin"),   # 단일값 또는 "all"
    country: str = Query("US"),         # 단일값 또는 "all"
    category: str = Query("6014"),
    date_str: str = Query(None, alias="date"),
    limit: int = Query(100, le=200),
):
    """
    상위 광고주 리스트.
    platform/network/ad_types/country에 "all" 전달 시 병렬 팬아웃 후 병합.
    """
    # 플랫폼 목록 결정
    platforms = ["ios", "android"] if platform == "all" else [platform]

    # 네트워크 목록 결정
    def networks_for(p):
        base = get_networks(p)
        return base if network == "all" else [network]

    # ad_types 목록 결정
    ad_types_list = AD_TYPES if ad_types == "all" else [ad_types]

    # 국가 목록 결정
    countries = COUNTRIES if country == "all" else [country]

    # 카테고리: 플랫폼별 변환 (android는 slug 사용)
    # "all" → 최상위 전체 카테고리 사용 (iOS: 6014, Android: game)
    def category_for(p):
        if category == "all":
            return "game" if p == "android" else "6014"
        if platform == "all":
            # ios 기준 숫자 ID → android slug 변환
            if p == "android":
                ios_label = {v: k for k, v in IOS_CATEGORIES.items()}.get(category)
                if ios_label:
                    return ANDROID_CATEGORIES.get(ios_label, "game")
                return "game"
        return category

    # 모든 조합으로 병렬 호출
    tasks = [
        fetch_safe(p, at, net, c, category_for(p), date_str, limit)
        for p in platforms
        for net in networks_for(p)
        for at in ad_types_list
        for c in countries
    ]

    if not tasks:
        return {"total": 0, "advertisers": []}

    results = await asyncio.gather(*tasks)
    errors = [r["error"] for r in results if r["error"]]
    ad_units = merge_ad_units([r["units"] for r in results])

    # 앱별 그룹핑 — API 반환 순서(impression 기반)를 랭킹으로 사용
    app_map: dict = {}
    for unit in ad_units:
        app_id = unit["app_id"]
        info = unit.get("app_info", {})
        if app_id not in app_map:
            app_map[app_id] = {
                "app_id": app_id,
                "name": info.get("humanized_name") or info.get("name", "Unknown"),
                "publisher_name": info.get("publisher_name", ""),
                "icon_url": info.get("icon_url", ""),
                "os": info.get("os", platform),
                "creative_count": 0,
                "ad_units": [],
                "_first_idx": len(app_map),  # API 반환 순서 보존
            }
        app_map[app_id]["creative_count"] += 1  # ad_unit당 소재 1개 표시 기준
        app_map[app_id]["ad_units"].append(unit)

    # API 순서(= impression 기반 SOV 순) 유지
    ranked = sorted(app_map.values(), key=lambda x: x["_first_idx"])
    for i, item in enumerate(ranked):
        item["rank"] = i + 1
        del item["_first_idx"]

    return {
        "total": len(ranked), "advertisers": ranked,
        "failed_calls": len(errors), "errors": errors[:5],
    }


@app.get("/api/top-creatives")
async def top_creatives(
    platform: str = Query("ios"),
    ad_types: str = Query("video"),
    network:  str = Query("Applovin"),
    country:  str = Query("US"),
    category: str = Query("6014"),
    date_str: str = Query(None, alias="date"),
    limit:    int = Query(100, le=200),
):
    """상위 광고 소재 flat 리스트 (앱 그룹핑 없이 impression 순)"""
    platforms      = ["ios", "android"] if platform == "all" else [platform]
    ad_types_list  = AD_TYPES if ad_types == "all" else [ad_types]
    countries      = COUNTRIES if country == "all" else [country]

    def networks_for(p):
        return get_networks(p) if network == "all" else [network]

    def category_for(p):
        if category == "all":
            return "game" if p == "android" else "6014"
        if platform == "all" and p == "android":
            ios_label = {v: k for k, v in IOS_CATEGORIES.items()}.get(category)
            return ANDROID_CATEGORIES.get(ios_label, "game") if ios_label else "game"
        return category

    tasks = [
        fetch_safe(p, at, net, c, category_for(p), date_str, limit)
        for p in platforms
        for net in networks_for(p)
        for at in ad_types_list
        for c in countries
    ]
    if not tasks:
        return {"total": 0, "creatives": []}

    results = await asyncio.gather(*tasks)
    errors = [r["error"] for r in results if r["error"]]
    ad_units = merge_ad_units([r["units"] for r in results])

    flat = []
    for i, unit in enumerate(ad_units):
        creative = (unit.get("creatives") or [None])[0]
        if not creative:
            continue
        flat.append({
            "rank":     i + 1,
            "creative": creative,
            "unit": {k: v for k, v in unit.items() if k != "creatives"},
            "app_info": unit.get("app_info", {}),
        })

    return {
        "total": len(flat), "creatives": flat,
        "failed_calls": len(errors), "errors": errors[:5],
    }


SNAPSHOT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots.db")

# 순위는 조합(네트워크×국가×플랫폼×유형)마다 따로 매겨지고 조합 크기도 제각각이라
# (2개짜리 조합의 1위 vs 91개짜리 조합의 1위) 절대 순위는 비교가 불가능하다.
# → 조합 내 백분위(pct)로 환산하고, 모수가 너무 작은 조합은 통계적으로 무의미하므로 제외한다.
MIN_COMBO_SIZE = 20

SNAP_CTE = f"""
WITH combo_sizes AS (
    SELECT week, platform, network, country, ad_type, COUNT(*) AS combo_n
    FROM weekly_snapshots GROUP BY 1, 2, 3, 4, 5
),
snap AS (
    SELECT s.*, c.combo_n, (s.rank * 100.0 / c.combo_n) AS pct
    FROM weekly_snapshots s
    JOIN combo_sizes c
      ON s.week = c.week AND s.platform = c.platform AND s.network = c.network
     AND s.country = c.country AND s.ad_type = c.ad_type
    WHERE c.combo_n >= {MIN_COMBO_SIZE} AND s.unit_id != ''
)
"""


@app.get("/api/creative-history")
async def creative_history(unit_ids: str = Query(...)):
    """주별 스냅샷 DB에서 소재별 top100 잔류 이력 조회 (backfill.py로 수집).
    반환: {unit_id: {weeks, first_week, last_week, best_rank}}"""
    ids = [i.strip() for i in unit_ids.split(",") if i.strip()][:500]
    if not ids or not os.path.exists(SNAPSHOT_DB):
        return {"history": {}}

    def query():
        conn = sqlite3.connect(SNAPSHOT_DB)
        try:
            qmarks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"""{SNAP_CTE}
                    SELECT unit_id, COUNT(DISTINCT week), MIN(week), MAX(week),
                           ROUND(MIN(pct), 1)
                    FROM snap WHERE unit_id IN ({qmarks}) GROUP BY unit_id""",
                ids,
            ).fetchall()
        finally:
            conn.close()
        return {
            r[0]: {"weeks": r[1], "first_week": r[2], "last_week": r[3], "best_pct": r[4]}
            for r in rows
        }

    # sqlite는 동기 → 이벤트 루프 블로킹 방지
    history = await asyncio.to_thread(query)
    return {"history": history}


# 누적 상위 소재 등급 → 최소 등장 주 수
TIER_MIN_WEEKS = {"half": 24, "quarter": 12, "month": 4}

# 네트워크 노출 우선순위 (파셋 칩 정렬용 — 집행 매체 우선)
NETWORK_PRIORITY = {n: i for i, n in enumerate(IOS_NETWORKS)}


@app.get("/api/cumulative-top")
async def cumulative_top(
    tier: str = Query("half"),          # half(반기 24주+) | quarter(분기 12주+) | month(월간 4주+)
    network: str = Query("all"),
    country: str = Query("all"),
    platform: str = Query("all"),
    ad_types: str = Query("all"),
    limit: int = Query(300, le=1000),
):
    """누적 상위 소재 — 스냅샷 DB에서 '오래 살아남은' 소재를 등급별로 반환.
    센서타워 호출 없이 로컬 DB만 조회하므로 쿼터 소모가 없다."""
    min_weeks = TIER_MIN_WEEKS.get(tier)
    if min_weeks is None:
        raise HTTPException(status_code=400, detail=f"알 수 없는 등급: {tier}")
    if not os.path.exists(SNAPSHOT_DB):
        return {"total": 0, "creatives": [], "tier": tier, "min_weeks": min_weeks}

    sel = {"network": network, "country": country, "platform": platform, "ad_type": ad_types}

    def facets(conn):
        """차원별 선택 가능 값과 개수. 각 차원은 '자기 자신을 제외한' 나머지 필터만 적용한다
        (= 이 값을 고르면 몇 개가 나오는지)."""
        out = {}
        for dim in sel:
            where, params = ["unit_id != ''"], []
            for col, val in sel.items():
                if col != dim and val != "all":
                    where.append(f"{col} = ?")
                    params.append(val)
            w = " AND ".join(where)
            rows = conn.execute(
                f"""{SNAP_CTE}
                    SELECT d, COUNT(*) FROM (
                        SELECT {dim} AS d, unit_id, COUNT(DISTINCT week) wk
                        FROM snap WHERE {w}
                        GROUP BY {dim}, unit_id HAVING wk >= ?
                    ) GROUP BY d ORDER BY 2 DESC""",
                params + [min_weeks],
            ).fetchall()
            if dim == "network":
                # 네트워크는 건수가 아니라 매체 우선순위 순으로 노출
                rows = sorted(rows, key=lambda r: NETWORK_PRIORITY.get(r[0], 99))
            total = conn.execute(
                f"""{SNAP_CTE}
                    SELECT COUNT(*) FROM (
                        SELECT unit_id, COUNT(DISTINCT week) wk
                        FROM snap WHERE {w}
                        GROUP BY unit_id HAVING wk >= ?
                    )""",
                params + [min_weeks],
            ).fetchone()[0]
            out[dim] = {"all": total, "values": [{"value": r[0], "count": r[1]} for r in rows]}
        return out

    def query():
        where, params = ["unit_id != ''"], []
        for col, val in sel.items():
            if val != "all":
                where.append(f"{col} = ?")
                params.append(val)

        sql = f"""
            {SNAP_CTE}
            SELECT unit_id,
                   COUNT(DISTINCT week) AS weeks,
                   ROUND(MIN(pct), 1) AS best_pct,
                   ROUND(AVG(pct), 1) AS avg_pct,
                   MIN(week)  AS first_week,
                   MAX(week)  AS last_week,
                   MIN(first_seen_at) AS first_seen_at,
                   MAX(last_seen_at)  AS last_seen_at,
                   MAX(app_id) AS app_id, MAX(app_name) AS app_name,
                   MAX(publisher) AS publisher, MAX(icon_url) AS icon_url,
                   MAX(ad_type) AS ad_type,
                   MAX(creative_id) AS creative_id, MAX(creative_url) AS creative_url,
                   MAX(thumb_url) AS thumb_url, MAX(preview_url) AS preview_url,
                   MAX(video_duration) AS video_duration,
                   MAX(width) AS width, MAX(height) AS height,
                   GROUP_CONCAT(DISTINCT network) AS networks,
                   GROUP_CONCAT(DISTINCT country) AS countries
            FROM snap
            WHERE {' AND '.join(where)}
            GROUP BY unit_id
            HAVING weeks >= ?
            ORDER BY weeks DESC, avg_pct ASC
            LIMIT ?
        """
        conn = sqlite3.connect(SNAPSHOT_DB)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params + [min_weeks, limit])], facets(conn)
        finally:
            conn.close()

    rows, facet_data = await asyncio.to_thread(query)

    items = []
    for i, r in enumerate(rows):
        # 등장 주 수 == 기간 폭이면 연속 집행, 작으면 중간에 빠진 구간이 있음
        span = (date.fromisoformat(r["last_week"]) - date.fromisoformat(r["first_week"])).days // 7 + 1
        items.append({
            "rank": i + 1,
            "creative": {
                "id": r["creative_id"], "creative_url": r["creative_url"],
                "thumb_url": r["thumb_url"], "preview_url": r["preview_url"],
                "video_duration": r["video_duration"],
                "width": r["width"], "height": r["height"],
            },
            "unit": {
                "id": r["unit_id"], "app_id": r["app_id"], "ad_type": r["ad_type"],
                "network": r["networks"], "country": r["countries"],
                "first_seen_at": r["first_seen_at"], "last_seen_at": r["last_seen_at"],
                "ad_formats": [],
            },
            "app_info": {
                "app_id": r["app_id"], "name": r["app_name"],
                "publisher_name": r["publisher"], "icon_url": r["icon_url"],
            },
            "stats": {
                "weeks": r["weeks"], "span": span, "consecutive": r["weeks"] >= span,
                "best_pct": r["best_pct"], "avg_pct": r["avg_pct"],
                "first_week": r["first_week"], "last_week": r["last_week"],
            },
        })

    return {
        "total": len(items), "creatives": items,
        "tier": tier, "min_weeks": min_weeks, "facets": facet_data,
    }


RISE_THRESHOLD = 20   # 급상승 판정: 지난주 대비 백분위 상승 폭(%p)
LONGRUN_WEEKS  = 4    # 롱런 진입 판정: 이 주 수를 이번 주에 달성
BRIEF_LIMIT    = 30   # 섹션당 최대 노출 수


@app.get("/api/weekly-brief")
async def weekly_brief(week: str = Query(None)):
    """주간 브리핑 — 특정 주에 '무슨 일이 일어났는지'를 스냅샷 DB 비교로 산출.
    신규 진입 / 급상승 / 롱런 진입 / 이탈 4개 섹션."""
    if not os.path.exists(SNAPSHOT_DB):
        return {"week": None, "weeks": [], "sections": {}}

    def query():
        conn = sqlite3.connect(SNAPSHOT_DB)
        conn.row_factory = sqlite3.Row
        try:
            weeks = [r[0] for r in conn.execute(
                f"{SNAP_CTE} SELECT DISTINCT week FROM snap ORDER BY week DESC")]
            if not weeks:
                return None, [], {}, {}
            cur_week = week if week in weeks else weeks[0]
            prev_week = (date.fromisoformat(cur_week) - timedelta(days=7)).isoformat()

            def ranks(w):   # unit_id → 그 주의 최고 백분위(작을수록 상위)
                return {r[0]: round(r[1], 1) for r in conn.execute(
                    f"{SNAP_CTE} SELECT unit_id, MIN(pct) FROM snap "
                    "WHERE week = ? GROUP BY unit_id", (w,))}

            def weeks_upto(w):  # unit_id → 해당 주까지의 누적 등장 주 수
                return {r[0]: r[1] for r in conn.execute(
                    f"{SNAP_CTE} SELECT unit_id, COUNT(DISTINCT week) FROM snap "
                    "WHERE week <= ? GROUP BY unit_id", (w,))}

            cur, prev = ranks(cur_week), ranks(prev_week)
            up_cur, up_prev = weeks_upto(cur_week), weeks_upto(prev_week)

            # ── 섹션별 후보 산출 ───────────────────────────────
            new_in = sorted(
                ((u, r) for u, r in cur.items() if up_cur.get(u, 0) == 1),
                key=lambda x: x[1])[:BRIEF_LIMIT]

            rising = sorted(
                ((u, r, prev[u], round(prev[u] - r, 1)) for u, r in cur.items()
                 if u in prev and prev[u] - r >= RISE_THRESHOLD),
                key=lambda x: -x[3])[:BRIEF_LIMIT]

            longrun = sorted(
                ((u, r) for u, r in cur.items() if up_cur.get(u, 0) == LONGRUN_WEEKS),
                key=lambda x: x[1])[:BRIEF_LIMIT]

            dropped = sorted(
                ((u, prev[u], up_prev.get(u, 0)) for u in prev
                 if u not in cur and up_prev.get(u, 0) >= LONGRUN_WEEKS),
                key=lambda x: -x[2])[:BRIEF_LIMIT]

            # ── 리포트 ①②③ ──────────────────────────────────
            # unit별 동시 집행 조합 수 + 앱 정보
            meta = {r[0]: {"combos": r[1], "app_id": r[2], "app_name": r[3]}
                    for r in conn.execute(
                        f"""{SNAP_CTE}
                            SELECT unit_id,
                                   COUNT(DISTINCT network||'|'||country||'|'||platform||'|'||ad_type),
                                   MAX(app_id), MAX(app_name)
                            FROM snap WHERE week = ? GROUP BY unit_id""", (cur_week,))}

            # 광고주별 소재 수 (이번 주 vs 지난주)
            app_cnt = {r[0]: {"name": r[1], "cur": r[2], "prev": r[3]} for r in conn.execute(
                f"""{SNAP_CTE}
                    SELECT app_id, MAX(app_name),
                           COUNT(DISTINCT CASE WHEN week = ? THEN unit_id END),
                           COUNT(DISTINCT CASE WHEN week = ? THEN unit_id END)
                    FROM snap WHERE week IN (?, ?) GROUP BY app_id""",
                (cur_week, prev_week, cur_week, prev_week))}

            def mover(a):
                d = a["cur"] - a["prev"]
                g = round(d / a["prev"] * 100) if a["prev"] else None
                return {"name": a["name"], "cur": a["cur"], "prev": a["prev"], "delta": d, "growth": g}

            movers_up = sorted(
                (mover(a) for a in app_cnt.values() if a["prev"] >= 3 and a["cur"] > a["prev"]),
                key=lambda x: -x["delta"])[:5]
            movers_down = sorted(
                (mover(a) for a in app_cnt.values() if a["prev"] >= 5 and a["cur"] < a["prev"]),
                key=lambda x: x["delta"])[:5]

            # ⭐ 꼭 봐야 할 소재 — 세 종류 신호를 점수화 후 상위 5개 (앱당 1개)
            surge_apps = {m["name"]: m for m in movers_up}
            picks = []
            for u, p in new_in:
                if p <= 5:
                    c = meta.get(u, {}).get("combos", 1)
                    picks.append((100 - p * 4 + (c - 1) * 10, u, p,
                                  f"신규 진입 즉시 상위 {p:g}%"
                                  + (f" · {c}개 조합 동시 집행" if c >= 2 else "")))
            for u, r, pv, dl in rising:
                if dl >= 40:
                    picks.append((50 + dl * 0.4, u, r,
                                  f"상위 {pv:g}% → {r:g}% 급등 (▲{dl:g}%p)"))
            for u, p in new_in:
                nm = meta.get(u, {}).get("app_name")
                m = surge_apps.get(nm)
                if m and m["delta"] >= 8:
                    picks.append((40 + min(m["growth"] or 0, 300) * 0.1, u, p,
                                  f"소재 {m['prev']}→{m['cur']}개 대량 증설 · 그중 최상위"))

            picks.sort(key=lambda x: -x[0])
            must, seen_app = [], set()
            for score, u, p, reason in picks:
                app = meta.get(u, {}).get("app_id")
                if u in {m[0] for m in must} or app in seen_app:
                    continue
                seen_app.add(app)
                must.append((u, p, reason))
                if len(must) == 5:
                    break

            # ⚠️ 이상 신호 — 직전 4주 평균 대비 이탈
            hist = conn.execute(
                f"""{SNAP_CTE}
                    SELECT week, COUNT(DISTINCT unit_id), COUNT(DISTINCT app_id)
                    FROM snap WHERE week <= ? GROUP BY week ORDER BY week DESC LIMIT 5""",
                (cur_week,)).fetchall()
            anomalies = []
            if len(hist) == 5:
                for idx, label in ((1, "소재 수"), (2, "광고주 수")):
                    now = hist[0][idx]
                    base = sum(h[idx] for h in hist[1:]) / 4
                    if base:
                        dev = round((now - base) / base * 100)
                        if abs(dev) >= 20:
                            anomalies.append({
                                "label": label, "now": now, "base": round(base),
                                "dev": dev,
                            })

            # must-watch 소재의 LLM 분석 결과 (있는 경우만 — 주별 수동/스케줄 분석으로 적재됨)
            analysis_map = {}
            try:
                if must:
                    qm2 = ",".join("?" * len(must))
                    for r in conn.execute(
                        f"""SELECT unit_id, hook_type, first_3s, visual_summary,
                                   why_hypothesis, confidence
                            FROM creative_analysis
                            WHERE week = ? AND unit_id IN ({qm2})""",
                        [cur_week] + [m[0] for m in must]):
                        analysis_map[r[0]] = {
                            "hook_type": r[1], "first_3s": r[2], "visual_summary": r[3],
                            "why_hypothesis": r[4], "confidence": r[5],
                        }
            except sqlite3.OperationalError:
                pass  # 테이블 없으면 분석 없이 표시

            # ── 카드 렌더용 상세 정보 일괄 조회 ────────────────
            ids = {x[0] for grp in (new_in, rising, longrun, dropped, must) for x in grp}
            details = {}
            if ids:
                qm = ",".join("?" * len(ids))
                for r in conn.execute(f"""
                    SELECT unit_id, MAX(app_id) app_id, MAX(app_name) app_name,
                           MAX(publisher) publisher, MAX(icon_url) icon_url,
                           MAX(ad_type) ad_type, MAX(creative_id) creative_id,
                           MAX(creative_url) creative_url, MAX(thumb_url) thumb_url,
                           MAX(preview_url) preview_url, MAX(video_duration) video_duration,
                           MAX(width) width, MAX(height) height,
                           MIN(first_seen_at) first_seen_at, MAX(last_seen_at) last_seen_at,
                           GROUP_CONCAT(DISTINCT network) networks,
                           GROUP_CONCAT(DISTINCT country) countries
                    FROM weekly_snapshots WHERE unit_id IN ({qm}) AND unit_id != '' GROUP BY unit_id
                """, list(ids)):
                    details[r["unit_id"]] = dict(r)

            return cur_week, weeks, {
                "new": new_in, "rising": rising, "longrun": longrun, "dropped": dropped,
                "must": must,
            }, details, {
                "movers_up": movers_up, "movers_down": movers_down, "anomalies": anomalies,
                "analysis_map": analysis_map,
            }
        finally:
            conn.close()

    cur_week, weeks, raw, details, report = await asyncio.to_thread(query)
    if cur_week is None:
        return {"week": None, "weeks": [], "sections": {}}

    def to_item(unit_id, pct, extra):
        d = details.get(unit_id, {})
        return {
            "pct": pct,
            "creative": {
                "id": d.get("creative_id"), "creative_url": d.get("creative_url"),
                "thumb_url": d.get("thumb_url"), "preview_url": d.get("preview_url"),
                "video_duration": d.get("video_duration"),
                "width": d.get("width"), "height": d.get("height"),
            },
            "unit": {
                "id": unit_id, "app_id": d.get("app_id"), "ad_type": d.get("ad_type"),
                "network": d.get("networks"), "country": d.get("countries"),
                "first_seen_at": d.get("first_seen_at"), "last_seen_at": d.get("last_seen_at"),
                "ad_formats": [],
            },
            "app_info": {
                "app_id": d.get("app_id"), "name": d.get("app_name"),
                "publisher_name": d.get("publisher"), "icon_url": d.get("icon_url"),
            },
            **extra,
        }

    sections = {
        "new":     [to_item(u, r, {}) for u, r in raw["new"]],
        "rising":  [to_item(u, r, {"prev_pct": p, "delta": dl}) for u, r, p, dl in raw["rising"]],
        "longrun": [to_item(u, r, {"weeks": LONGRUN_WEEKS}) for u, r in raw["longrun"]],
        "dropped": [to_item(u, r, {"weeks": w}) for u, r, w in raw["dropped"]],
    }
    analysis_map = report.pop("analysis_map", {})
    report["must_watch"] = [
        to_item(u, p, {"reason": rs, "analysis": analysis_map.get(u)})
        for u, p, rs in raw["must"]
    ]
    return {
        "week": cur_week, "weeks": weeks, "sections": sections, "report": report,
        "rise_threshold": RISE_THRESHOLD, "longrun_weeks": LONGRUN_WEEKS,
    }


@app.get("/api/download")
async def download(url: str = Query(...), filename: str = Query("creative")):
    """S3 소재를 프록시하여 브라우저에 직접 다운로드"""
    parsed = urllib.parse.urlparse(url)
    # S3 버킷 도메인만 허용 (*.s3.amazonaws.com)
    if not parsed.netloc.endswith(".s3.amazonaws.com"):
        raise HTTPException(status_code=400, detail="허용되지 않는 도메인입니다.")

    # S3에서 Content-Type을 먼저 확인 (HEAD 요청)
    async with httpx.AsyncClient(timeout=10) as hc:
        head = await hc.head(url)
        content_type = head.headers.get("content-type", "application/octet-stream")

    # Content-Type → 확장자 매핑 (파일명에 확장자 없을 때만 추가)
    KNOWN_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".html"}
    has_ext = any(filename.lower().endswith(e) for e in KNOWN_EXTS)
    if not has_ext:
        if "video" in content_type:
            filename += ".mp4"
        elif "jpeg" in content_type or "jpg" in content_type:
            filename += ".jpg"
        elif "png" in content_type:
            filename += ".png"
        elif "gif" in content_type:
            filename += ".gif"
        elif "webp" in content_type:
            filename += ".webp"
        elif "zip" in content_type:
            filename += ".zip"
        elif "html" in content_type:
            filename += ".html"

    encoded_name = urllib.parse.quote(filename, safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
        "Content-Type": content_type,
        "Cache-Control": "no-store",
    }

    async def stream():
        async with httpx.AsyncClient(timeout=60) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    yield chunk

    return StreamingResponse(stream(), media_type=content_type, headers=headers)


# ── Static Frontend ─────────────────────────────────────────
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/", response_class=FileResponse)
    async def index():
        return FileResponse(os.path.join(frontend_dir, "index.html"))

    # /index.html 직접 접근도 허용
    @app.get("/index.html", response_class=FileResponse)
    async def index_html():
        return FileResponse(os.path.join(frontend_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
