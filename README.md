# UA Intelligence V3

Sensor Tower 데이터를 기반으로 모바일 게임 광고 소재(Creative)를 분석하는 내부 대시보드입니다.

---

## 주요 기능

### 게임 UA 탭
- 네트워크 / 플랫폼 / 국가 / 카테고리 / 주 단위 날짜 필터
- 광고주 순위 리스트 (노출 기준)
- 광고주 클릭 시 해당 앱의 크리에이티브 목록 표시
- 소재 클릭 → 모달 팝업 (영상 재생 / 이미지 확인)
- 모달 내 좌우 화살표 및 키보드 ← → 로 소재 탐색
- 개별 다운로드 / 전체 다운로드 (이미지 `.jpg`, 영상 `.mp4` 자동 구분)

### 상위 소재 탭
- 네트워크 전체의 상위 광고 소재를 그리드 뷰로 표시
- 페이지당 소재 수 조절 가능 (기본 20개)
- 페이지네이션

### 지원 네트워크
`Adcolony` `Admob` `Applovin` `Chartboost` `Meta Audience Network` `Tapjoy` `TikTok` `Unity` `Vungle` `Youtube`

> Meta Audience Network는 Sensor Tower 내부 Unified API를 통해 데이터를 가져옵니다.

### 지원 광고 유형
`video` `image` `banner` `full_screen` `playable` `interactive-playable`

---

## 기술 스택

| 구분 | 기술 |
|------|------|
| Backend | FastAPI (Python) + httpx |
| Frontend | Vanilla JS / HTML / CSS (단일 파일) |
| 데이터 소스 | Sensor Tower Ad Intel API |
| 배포 | Vercel (프론트) / Render 권장 (API) |

---

## 로컬 실행

### 1. 환경 변수 설정

```bash
cp backend/.env.example backend/.env
# .env 파일에 토큰 입력
```

`backend/.env`:
```
SENSORTOWER_API_TOKEN=your_token_here
```

### 2. 패키지 설치 및 서버 실행

```bash
pip install -r requirements.txt
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 3. 접속

```
http://localhost:8000
```

---

## 배포

### Render (권장)

1. [render.com](https://render.com) → New Web Service → GitHub 연결
2. `render.yaml`이 자동 감지됨
3. Environment Variables에 `SENSORTOWER_API_TOKEN` 추가
4. Deploy

### Vercel

1. GitHub 연결 후 Import
2. Environment Variables에 `SENSORTOWER_API_TOKEN` 추가
3. Deploy

> **주의**: Vercel Hobby 플랜은 함수 실행 시간이 10초로 제한되어 콜드 스타트 시 타임아웃이 발생할 수 있습니다. Render 또는 Vercel Pro 사용을 권장합니다.

---

## 프로젝트 구조

```
UA_IntelligenceV3/
├── backend/
│   ├── main.py          # FastAPI 앱, API 엔드포인트
│   ├── sensortower.py   # Sensor Tower API 클라이언트
│   └── .env.example     # 환경변수 예시
├── frontend/
│   └── index.html       # 단일 페이지 앱 (SPA)
├── api/
│   └── index.py         # Vercel 서버리스 진입점
├── render.yaml          # Render 배포 설정
├── vercel.json          # Vercel 배포 설정
└── requirements.txt     # Python 패키지 목록
```
