#!/bin/bash

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
ENV_FILE="$PROJECT_ROOT/backend/.env"

# .env 파일 확인 (서버는 backend/ 에서 실행되므로 backend/.env 를 읽는다)
if [ ! -f "$ENV_FILE" ]; then
  echo "⚠️  backend/.env 파일이 없습니다. 생성합니다..."
  cat > "$ENV_FILE" <<EOF
SENSORTOWER_API_TOKEN=your_token_here
EOF
  echo "   → backend/.env 파일을 열어 SENSORTOWER_API_TOKEN을 설정하세요."
  echo "   → 그 후 다시 ./run.command 를 실행하세요."
  exit 1
fi

if grep -q "your_token_here" "$ENV_FILE"; then
  echo "❌ backend/.env 의 SENSORTOWER_API_TOKEN을 실제 토큰으로 교체하세요."
  exit 1
fi

# 가상환경 생성 (없을 경우)
if [ ! -d "$VENV_DIR" ]; then
  echo "📦 가상환경 생성 중..."
  python3 -m venv "$VENV_DIR"
fi

# 가상환경 활성화
source "$VENV_DIR/bin/activate"

# 의존성 설치
echo "📦 패키지 설치 확인 중..."
pip install -q -r "$PROJECT_ROOT/requirements.txt"

# 서버 실행
echo ""
echo "🚀 서버 시작: http://localhost:8000"
echo "   종료: Ctrl+C"
echo ""
cd "$PROJECT_ROOT/backend"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
