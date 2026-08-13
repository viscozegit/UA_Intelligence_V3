from __future__ import annotations
import httpx
import os
import urllib.parse
from datetime import date, timedelta
from typing import Optional

BASE_URL = "https://api.sensortower.com/v1"
UNIFIED_BASE_URL = "https://app.sensortower.com/api/unified"

# 실제로 데이터가 반환되는 네트워크만, 우리 집행 매체 우선순위 순으로 정렬
# (Meta > Youtube > TikTok이 주력 매체. Applovin·Unity는 게임 UA 트렌드 조기 신호원으로 유지)
# 제외: Adcolony·Tapjoy(서비스 종료), Chartboost(데이터 희박 + Meta와 unified 쿼터 경합)
IOS_NETWORKS = [
    "Meta Audience Network", "Youtube", "TikTok",
    "Applovin", "Unity", "Admob", "Vungle",
]

ANDROID_NETWORKS = [
    "Meta Audience Network", "Youtube", "TikTok",
    "Applovin", "Unity", "Admob", "Vungle",
]

AD_TYPES = [
    "video", "image", "banner", "full_screen",
    "playable", "interactive-playable",
]

# unified 엔드포인트에서 지원하는 추가 ad_types
UNIFIED_AD_TYPES = [
    "video", "video-other", "image", "image-banner", "image-other",
    "banner", "full_screen", "playable", "interactive-playable",
]

# unified 엔드포인트에서만 데이터가 반환되는 네트워크
# (TikTok은 public creatives/top이 200 OK + 항상 0건 → unified 필수)
UNIFIED_ONLY_NETWORKS = {"Meta Audience Network", "TikTok"}

# iOS: 숫자 카테고리 ID / Android: 문자열 카테고리 슬러그
# Soul Strike·ARES·Star Sailors 세 타이틀의 실제 등록 카테고리만 포함
# (Soul Strike·ARES: Action+Role Playing / Star Sailors: Role Playing+Strategy)
IOS_CATEGORIES = {
    "Games (All)": "6014",
    "Action": "7001",
    "Role Playing": "7014",
    "Strategy": "7017",
}

ANDROID_CATEGORIES = {
    "Games (All)": "game",
    "Action": "game_action",
    "Role Playing": "game_role_playing",
    "Strategy": "game_strategy",
}


class SensorTowerClient:
    def __init__(self):
        self.token = os.getenv("SENSORTOWER_API_TOKEN", "")

    async def get_top_creatives(
        self,
        platform: str = "ios",
        ad_types: str = "video",
        network: str = "Applovin",
        country: str = "US",
        category: str = "6014",     # iOS ID 또는 Android slug
        date_str: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """GET /v1/{ios|android}/ad_intel/creatives/top"""
        if date_str is None:
            date_str = (date.today() - timedelta(days=1)).isoformat()

        params = {
            "auth_token": self.token,
            "ad_types": ad_types,
            "network": network,
            "country": country,
            "category": category,
            "date": date_str,
            "limit": limit,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BASE_URL}/{platform}/ad_intel/creatives/top",
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_top_creatives_unified(
        self,
        platform: str = "ios",          # ios | android | all
        ad_types: str = "video",
        network: str = "Meta Audience Network",
        country: str = "US",
        category: str = "6014",         # iOS 카테고리 ID (unified 엔드포인트는 iOS ID 사용)
        date_str: Optional[str] = None,
        limit: int = 100,
        period: str = "month",          # week | month
    ) -> dict:
        """
        GET /api/unified/ad_intel/creatives/top
        Meta Audience Network 등 통합 엔드포인트에서만 지원되는 네트워크용.
        """
        # 날짜 처리: period에 맞춰 시작일로 스냅
        # month → 해당 월 1일 / week → 해당 주 월요일
        if date_str is None:
            base = date.today()
        else:
            parts = date_str.split("-")
            base = date(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 1)

        if period == "week":
            start = base - timedelta(days=base.weekday())
        else:
            start = base.replace(day=1)

        iso_date = start.strftime("%Y-%m-%dT00:00:00.000Z")

        # unified 엔드포인트는 iOS 숫자 카테고리 ID만 받는다
        # → android slug가 들어오면 변환 (예: game_puzzle → 7011)
        if not str(category).isdigit():
            label = {v: k for k, v in ANDROID_CATEGORIES.items()}.get(str(category))
            category = IOS_CATEGORIES.get(label, "6014") if label else "6014"

        # platform → devices 매핑
        if platform == "ios":
            devices = ["iphone", "ipad"]
        elif platform == "android":
            devices = ["android"]
        else:  # all
            devices = ["iphone", "android", "ipad"]

        # httpx는 리스트 파라미터를 자동으로 key[]=v1&key[]=v2 형태로 처리하지 않으므로
        # 수동으로 쿼리스트링 구성
        qs_parts = []
        qs_parts.append(f"auth_token={urllib.parse.quote(self.token)}")
        qs_parts.append(f"network={urllib.parse.quote(network)}")
        qs_parts.append(f"country={urllib.parse.quote(country)}")
        qs_parts.append(f"category={urllib.parse.quote(str(category))}")
        qs_parts.append(f"date={urllib.parse.quote(iso_date)}")
        qs_parts.append(f"period={period}")
        qs_parts.append(f"limit={limit}")
        qs_parts.append("page=1")
        qs_parts.append("new_creatives_only=false")

        for d in devices:
            qs_parts.append(f"devices[]={urllib.parse.quote(d)}")

        # ad_types 처리
        at = ad_types
        if at == "video":
            ad_type_list = ["video", "video-other"]
        elif at == "image":
            ad_type_list = ["image", "image-banner", "image-other"]
        else:
            ad_type_list = [at]

        for a in ad_type_list:
            qs_parts.append(f"ad_types[]={urllib.parse.quote(a)}")

        url = f"{UNIFIED_BASE_URL}/ad_intel/creatives/top?{'&'.join(qs_parts)}"

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            # unified 엔드포인트 응답은 공개 API와 동일한 구조이지만
            # 최상위 키가 ad_units 대신 다를 수 있으므로 정규화
            if "ad_units" not in data and isinstance(data, list):
                data = {"ad_units": data}
            elif "ad_units" not in data:
                data["ad_units"] = []
            return data
