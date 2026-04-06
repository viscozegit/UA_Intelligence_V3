from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import asyncio
import os
import httpx
import urllib.parse

load_dotenv()

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
    """에러 시 빈 리스트 반환. 각 unit에 network/country/platform 태그 추가."""
    try:
        if network in UNIFIED_ONLY_NETWORKS:
            # Meta Audience Network 등은 unified 엔드포인트 사용
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
        return units
    except Exception:
        return []


# ── API Routes ──────────────────────────────────────────────



@app.get("/api/meta")
async def meta(platform: str = Query("ios")):
    """플랫폼별 유효한 네트워크·카테고리·ad_types 반환"""
    if platform == "all":
        networks = sorted(set(IOS_NETWORKS) | set(ANDROID_NETWORKS))
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
    ad_units = merge_ad_units(results)

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

    return {"total": len(ranked), "advertisers": ranked}


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
    ad_units = merge_ad_units(results)

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

    return {"total": len(flat), "creatives": flat}


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


# ── Static Frontend (로컬 개발 전용, Vercel에서는 CDN이 서빙) ─
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/", response_class=FileResponse)
    async def index():
        return FileResponse(os.path.join(frontend_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
