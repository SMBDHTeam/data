# Python 3.12 기반 이미지
FROM python:3.12-slim

# 컨테이너 내부 작업 위치
WORKDIR /app

# Python 의존성 설치
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# FastAPI 코드 + 모델 파일 복사
COPY . .

# AI 서버 포트
EXPOSE 8010

# FastAPI 실행
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]