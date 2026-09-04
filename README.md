# FastAPI AI / Schedule Server

`data/` 는 부산 투어 프로젝트의 Python FastAPI 서버입니다.

현재 이 서버가 담당하는 기능은 크게 3가지입니다.

- 장소 카테고리 / 테마 분류
- 일정 미리보기 / 일정 생성 / 일정 조회 / 일정 수정 / 지도 응답
- 즉흥 여행 목적지 추천 / 즉흥 코스 생성

이 문서는 2026년 9월 4일 기준 현재 구조와 운영 상태를 빠르게 파악하기 위한 요약 문서입니다.

## 구조

- `server/`
  - 프론트가 직접 호출하는 공개 API 서버
  - 인증, 에러 포맷, 기존 API 계약 유지
  - 일정 API는 FastAPI(`data-ai`)에 위임 가능
- `data/`
  - FastAPI 서버
  - 분류 모델, 일정 생성 로직, 교통 응답 조립, 즉흥 여행 추천 담당

현재 운영 흐름은 아래와 같습니다.

1. 프론트는 기본적으로 `server` API를 호출합니다.
2. `server` 는 일정 관련 요청을 `data-ai` FastAPI로 위임할 수 있습니다.
3. `data/` 는 개발 DB와 외부 교통 API를 사용해 응답을 만듭니다.

## 제공 API

### 분류 API

- `GET /health`
- `POST /predict`
- `POST /predict/batch`

역할:

- 장소 텍스트 기반 1차 카테고리 분류
- Spring 호환 응답 필드 제공
- 일정/추천용 보조 테마 정보 생성

주요 응답 필드:

- `primary_category`
- `theme_answer_id`
- `model_category`
- `decision_source`
- `primary_theme`
- `secondary_themes`
- `semantic_tags`
- `mood_tags`
- `is_meal_place`
- `is_low_mobility_friendly`

### 일정 API

- `POST /api/v1/schedule-previews`
- `GET /api/v1/schedule-previews/{preview_id}`
- `POST /api/v1/schedules`
- `GET /api/v1/schedules`
- `GET /api/v1/schedules/{schedule_id}`
- `PATCH /api/v1/schedules/{schedule_id}`
- `GET /api/v1/schedules/{schedule_id}/map`

현재 상태:

- preview 생성 / 조회 가능
- preview 기반 일정 생성 가능
- 직접 일정 생성 가능
- 일정 목록 / 단건 조회 가능
- 일정 수정 가능
- 지도 응답 생성 가능

### 즉흥 여행 API

- `POST /api/v1/spontaneous-trips/destinations`
- `POST /api/v1/spontaneous-trips/course`

현재 상태:

- 출발 위치/시간/희망 테마 기준 목적지 추천 가능
- 선택한 목적지 기준 즉흥 코스 생성 가능
- 교통수단별 이동 가능 여부 반영
- stop별 시간 정보와 마지막 복귀 시간 포함

## 일정 생성 동작 방식

### 장소 후보 소스

일정 생성은 아래 우선순위로 후보 장소를 읽습니다.

1. `SPRING_DATASOURCE_URL`, `SPRING_DATASOURCE_USERNAME`, `SPRING_DATASOURCE_PASSWORD`
2. `LOCAL_POSTGRES_*`
3. `candidate_places.json` fallback

즉, 정상 env가 있으면 JSON이 아니라 개발 DB를 우선 사용합니다.

`/health` 확인값:

- `schedule_candidate_source`
- `schedule_candidate_count`
- `schedule_db_enabled`
- `schedule_db_host`

### 교통 / 경로

현재 교통 경로 생성은 아래 공급자를 사용합니다.

- ODSAY 대중교통 경로
- TMAP 보행 경로
- 내부 도보 fallback

관련 env:

- `ODSAY_ENABLED`
- `ODSAY_API_KEY`
- `ODSAY_BASE_URL`
- `TMAP_WALKING_ENABLED`
- `TMAP_BASE_URL`
- `SKT_API_KEY`

참고:

- 사용자 응답에서는 `TMAP`, `INTERNAL_WALK`, `FASTAPI_MIGRATION` 같은 공급자명을 최대한 숨기도록 정리했습니다.
- 기존 DB에 저장된 일정 조회도 같은 기준으로 정규화합니다.

### 일정 채우기 보정

현재 일정 생성은 하루가 너무 일찍 끝나는 경우를 줄이기 위해 아래 보정을 사용합니다.

- 목표 방문지 수 계산
- underfilled day 판정
- 방문지 추가 재배치 시도
- 일부 방문지 체류 시간 연장

주의:

- `10:00 ~ 20:00` 는 활동 가능 시간 창입니다.
- 항상 20시까지 꽉 차는 것은 아닙니다.
- 다만 긴 일정은 방문지 수와 체류시간을 더 적극적으로 채우도록 보정하고 있습니다.

### AI 재정렬

현재 사용하지 않습니다.

이전 실험용 후보 재정렬 경로는 제거했고, 지금 일정 생성은 규칙 기반 점수와 시간/이동 가능 여부 검증으로 동작합니다.

## 운영 조건

### Spring -> FastAPI 위임 env

Spring 서버가 일정 API를 FastAPI에 위임하려면 아래 값이 필요합니다.

```env
SCHEDULE_FASTAPI_ENABLED=true
SCHEDULE_FASTAPI_BASE_URL=http://data-ai:8010
```

주의:

- `SCHEDULE_FASTAPI_ENABLED` 가 없으면 위임이 비활성화될 수 있습니다.
- `SCHEDULE_FASTAPI_BASE_URL` 은 끝에 슬래시 없이 넣는 것을 권장합니다.
- EC2에서 컨테이너를 수동 재실행할 때 env 누락 시 preview/create가 실패할 수 있습니다.

### FastAPI 컨테이너 env

FastAPI 컨테이너는 최소 아래 값들이 필요합니다.

```env
SPRING_DATASOURCE_URL=...
SPRING_DATASOURCE_USERNAME=...
SPRING_DATASOURCE_PASSWORD=...
ODSAY_ENABLED=true
ODSAY_API_KEY=...
SKT_API_KEY=...
```

현재 [deploy-dev.yml](/Users/miju/test_1/data/.github/workflows/deploy-dev.yml) 은 위 값을 `.env.dev` 로 만든 뒤 EC2의 `data-ai` 컨테이너를 `--env-file /opt/hackathon-dev/.env.dev` 로 실행합니다.

## 즉흥 여행 실패 코드

### 목적지 추천

`POST /api/v1/spontaneous-trips/destinations`

- `DESTINATIONS_NOT_FOUND`

### 즉흥 코스

`POST /api/v1/spontaneous-trips/course`

- `DESTINATION_NOT_FOUND`
- `TRANSPORT_MODE_UNAVAILABLE`
- `PLACE_SEARCH_FAILED`
- `COURSE_CANDIDATES_NOT_FOUND`
- `COURSE_GENERATION_EMPTY`
- `COURSE_SEGMENT_ROUTE_UNAVAILABLE`
- `COURSE_RETURN_ROUTE_UNAVAILABLE`

프론트 문구 예시:

- `TRANSPORT_MODE_UNAVAILABLE`
  - 선택한 이동수단으로 이동 가능한 코스를 찾지 못했습니다.
- `COURSE_CANDIDATES_NOT_FOUND`
  - 주변에서 추천할 장소를 찾지 못했습니다.
- `COURSE_SEGMENT_ROUTE_UNAVAILABLE`
  - 추천 코스의 이동 경로를 계산하지 못했습니다. 다시 시도해 주세요.
- `COURSE_RETURN_ROUTE_UNAVAILABLE`
  - 복귀 경로를 계산하지 못했습니다. 다른 이동수단으로 다시 시도해 주세요.

## 로컬 실행

```bash
cd /Users/miju/test_1/data
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8010
```

## 빠른 확인 명령

### health

```bash
curl -s http://127.0.0.1:8010/health
```

### 카테고리 분류

```bash
curl -s -X POST http://127.0.0.1:8010/predict \
  -H 'Content-Type: application/json' \
  --data '{
    "title": "광안리해수욕장",
    "overview": "부산 대표 해변과 야경 명소",
    "contenttypeid": "12",
    "cat3": "A01011100",
    "addr1": "부산 수영구 광안해변로 219",
    "mapx": 129.1186,
    "mapy": 35.1532
  }'
```

### 일정 미리보기

```bash
curl -s -X POST http://127.0.0.1:8010/api/v1/schedule-previews \
  -H 'Content-Type: application/json' \
  -d '{
    "startDate": "2026-08-10",
    "endDate": "2026-08-11",
    "startLocation": {
      "name": "부산역",
      "longitude": 129.0403,
      "latitude": 35.1151
    },
    "lodgingPlan": {
      "mode": "UNDECIDED",
      "nightStays": []
    },
    "selectedAnswers": [
      { "questionId": "THEME", "answerIds": ["THEME_NATURE"] },
      { "questionId": "COMPANION", "answerIds": ["COMPANION_FRIENDS"] },
      { "questionId": "MOBILITY", "answerIds": ["MOBILITY_NORMAL"] },
      { "questionId": "PACE", "answerIds": ["PACE_RELAXED"] },
      { "questionId": "TRANSIT", "answerIds": ["TRANSIT_SIMPLE"] }
    ],
    "mustVisitPlaceIds": [],
    "fixedEvents": [],
    "dayOverrides": []
  }'
```

## 파일 역할

- [app.py](/Users/miju/test_1/data/app.py)
  - FastAPI 진입점
- [schedule/service.py](/Users/miju/test_1/data/schedule/service.py)
  - 일정 생성 핵심 로직
- [schedule/preview_service.py](/Users/miju/test_1/data/schedule/preview_service.py)
  - 일정 preview 생성 / 조회
- [schedule/persistence.py](/Users/miju/test_1/data/schedule/persistence.py)
  - preview / schedule DB 저장 및 조회
- [transit/routing.py](/Users/miju/test_1/data/transit/routing.py)
  - ODSAY / TMAP / 도보 fallback 경로 생성
- [spontaneous/course.py](/Users/miju/test_1/data/spontaneous/course.py)
  - 즉흥 코스 조합
- [spontaneous/routing.py](/Users/miju/test_1/data/spontaneous/routing.py)
  - 즉흥 여행 이동시간 계산
- [spontaneous/places.py](/Users/miju/test_1/data/spontaneous/places.py)
  - 즉흥 여행 후보 장소 조회 / 필터링
- [core/runtime_env.py](/Users/miju/test_1/data/core/runtime_env.py)
  - env 파일 로딩

## 현재 한 줄 요약

현재 `data/` 는 단순 분류 서버가 아니라, 분류 + 일정 preview/create/list/get/update/map + 즉흥 여행 추천/코스를 담당하는 FastAPI 서버입니다.

다만 공개 API의 최종 진입점은 아직 Spring이 일부 맡고 있으므로, 현재 단계는 하이브리드 운영 상태입니다.
