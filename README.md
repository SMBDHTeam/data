# FastAPI AI / Schedule Server

`data/` 는 부산 투어 프로젝트의 Python FastAPI 서버입니다.

원래는 Spring `server/` 안에 있던 일부 AI/일정 생성 관련 기능을 분리해서,

- 카테고리 분류 / 테마 추론
- 일정 미리보기 / 일정 생성
- 일정 경로 응답 조립

을 Python 서버에서 담당하도록 옮기고 있습니다.

이 문서는 현재 마이그레이션 상태를 팀원이 빠르게 이해할 수 있게 정리한 문서입니다.

## 현재 구조

- `server/`
  - 현재 프론트가 직접 붙는 공개 API 서버
  - 인증, 에러 포맷, 기존 API 계약 유지
  - 일정 관련 API는 점진적으로 FastAPI에 위임 중
- `data/`
  - 카테고리 분류 모델 + 일정 생성 FastAPI 서버
  - 장소 후보 조회, 일정 조합, 교통 응답 생성 담당

현재 운영 기준으로는:

1. 프론트는 여전히 `server` API를 호출할 수 있음
2. `server` 는 일정 API 요청을 `data-ai` FastAPI 로 위임할 수 있음
3. `data/` 는 개발 DB에서 장소 후보를 읽어 일정 응답을 생성함

## 무엇을 옮겼는가

### 1. 카테고리 / 테마 분류

FastAPI가 아래 엔드포인트를 제공함:

- `GET /health`
- `POST /predict`
- `POST /predict/batch`

역할:

- 장소 텍스트 기반 1차 카테고리 분류
- Spring 호환 필드 응답
- 추천/일정용 부가 필드 생성

현재 응답에는 아래 성격의 값이 포함됩니다.

- Spring 호환 필드
  - `primary_category`
  - `theme_answer_id`
  - `model_category`
  - `decision_source`
- 확장 분석 필드
  - `primary_theme`
  - `secondary_themes`
  - `semantic_tags`
  - `mood_tags`
  - `is_meal_place`
  - `is_low_mobility_friendly`
  - `cluster_key`
  - `reason`

### 2. 일정 미리보기 API

FastAPI가 아래 엔드포인트를 제공함:

- `POST /api/v1/schedule-previews`
- `GET /api/v1/schedule-previews/{preview_id}`

현재 상태:

- Spring에 있던 preview 계산 흐름을 FastAPI로 이전
- 요청 검증 후 preview 생성 가능
- preview 조회 가능
- Spring에서는 현재 이 API를 직접 계산하지 않고 FastAPI 위임 구조로 정리 중

### 3. 일정 생성 / 조회 / 수정 / 지도 API

FastAPI가 아래 엔드포인트를 제공함:

- `POST /api/v1/schedules`
- `GET /api/v1/schedules`
- `GET /api/v1/schedules/{schedule_id}`
- `PATCH /api/v1/schedules/{schedule_id}`
- `GET /api/v1/schedules/{schedule_id}/map`

현재 상태:

- 일정 생성 가능
- preview 기반 일정 생성 가능
- 일정 목록 / 단건 조회 가능
- 일정 수정 가능
- 지도 응답 생성 가능

Spring `ScheduleService`, `SchedulePreviewService` 는 현재 실제 생성 로직을 거의 수행하지 않고,
FastAPI 호출을 위한 위임 레이어로 정리되고 있습니다.

### 4. 즉흥 여행 추천 / 코스 API

FastAPI가 아래 엔드포인트를 제공함:

- `POST /api/v1/spontaneous-trips/destinations`
- `POST /api/v1/spontaneous-trips/course`

현재 상태:

- 출발 위치/시간/희망 테마 기준 목적지 추천 가능
- 선택한 목적지 기준 즉흥 코스 생성 가능
- 교통수단별 이동 가능 여부를 반영해 추천/코스 생성
- 코스 stop 에 `arriveAt`, `departAt`, `inboundMinutes` 포함
- 마지막 복귀 시간(`finalReturnMinutes`, `expectedReturnAt`) 포함

최근 보강된 점:

- 목적지 추천은 전체 후보를 끝까지 평가한 뒤 최종 점수 기준 `top 5`만 반환
- 즉흥 코스는 구간 이동시간을 못 찾으면 `0분`으로 땜질하지 않고 실패 처리
- 복귀 경로를 못 찾는 경우도 명확한 에러 코드로 실패 처리
- 코스 장소 선택은 매 stop 마다 직전 선택 장소 기준으로 다시 점수 계산

## 현재 FastAPI 일정 생성 로직 상태

### 장소 후보 소스

현재 일정 생성은 먼저 개발 DB에서 장소 후보를 읽습니다.

우선순위:

1. `SPRING_DATASOURCE_URL`, `SPRING_DATASOURCE_USERNAME`, `SPRING_DATASOURCE_PASSWORD`
2. 로컬 PostgreSQL 계열 env (`LOCAL_POSTGRES_*`)
3. `candidate_places.json` fallback

즉, 정상 env가 있으면 JSON 저장 기반이 아니라 개발 DB를 우선 사용합니다.

`/health` 에서 아래 값으로 확인할 수 있습니다.

- `schedule_candidate_source`
  - `database`
  - `json_fallback`
- `schedule_candidate_count`

### 교통 / 경로

현재 `data/` 는 교통 경로 생성 시 다음을 사용합니다.

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

현재 응답에는 `provider`, `fallbackUsed`, `segments` 가 포함되어 있어
실제로 어떤 경로 공급자가 사용됐는지 확인할 수 있습니다.

### 일정 시간 채우기 보정

현재 일정 생성은 하루 종료 시각보다 너무 이르게 끝나는 경우를 줄이기 위해
underfilled day 보정 로직을 사용합니다.

현재 동작:

- 하루 사용 시간이 너무 짧으면 stop 수를 늘려 재계산 시도
- 과도하게 이른 종료 일정을 완화
- 그래도 후보/교통/운영시간 제약이 크면 20시 전에 끝날 수 있음

즉, `10:00 ~ 20:00` 는 “활동 가능 시간 창”이고,
항상 20시까지 꽉 채워진다는 보장은 아닙니다.

### AI 후보 재정렬

현재 일정 생성은 기본적으로 규칙 기반 점수로 후보 장소를 정렬합니다.

추가로 선택적으로 아래 흐름을 켤 수 있습니다.

1. 규칙 기반으로 상위 후보를 먼저 추림
2. LLM 이 상위 후보 일부를 하루 맥락 기준으로 재정렬
3. FastAPI 가 다시 시간/이동 가능 여부를 검증

즉, AI 가 전체 일정을 직접 확정하지 않고
"어떤 후보를 더 우선할지"만 보정합니다.

현재 env:

- `SCHEDULE_AI_RERANK_ENABLED`
- `SCHEDULE_AI_RERANK_API_KEY`
- `SCHEDULE_AI_RERANK_MODEL`
- `SCHEDULE_AI_RERANK_BASE_URL`
- `SCHEDULE_AI_RERANK_TOP_N`
- `SCHEDULE_AI_RERANK_TIMEOUT_SECONDS`

기본값은 비활성화이며,
AI 호출이 실패하면 기존 규칙 기반 정렬로 즉시 fallback 합니다.

`/health` 에서 아래 값으로 상태를 확인할 수 있습니다.

- `schedule_ai_rerank_enabled`
- `schedule_ai_rerank_configured`
- `schedule_ai_rerank_model`

## 지금 실제 운영에서 확인된 상태

### 완료된 것

- FastAPI 컨테이너에서 카테고리 분류 API 동작 확인
- FastAPI 컨테이너에서 일정 preview API 동작 확인
- FastAPI 컨테이너에서 일정 create/list/get/map API 동작 확인
- 개발 DB 기준 장소 후보 로딩 확인
- ODSAY 응답 포함 경로 생성 확인
- TMAP 보행 경로 코드 반영 완료
- Spring -> FastAPI 일정 위임 경로 연결 완료
- Spring 내부의 구 일정 planner / evaluation / AI planner client 레거시 코드 정리 완료
- `data-ai` 배포 workflow에 DB / ODSAY / TMAP env 전달 경로 반영 완료

### 반쯤 이전된 것

- 공개 API 진입점은 아직 Spring이 유지
- 프론트는 여전히 Spring API 계약에 의존하는 부분이 있음
- 운영 반영 후 실제 EC2 컨테이너가 새 workflow/env로 올라왔는지 확인이 필요함

### 아직 남은 것

- 배포 후 `server-dev`, `data-ai` 가 새 env 기준으로 정상 재기동되는지 확인
- 프론트가 현재 preview/create 응답과 에러 포맷을 안정적으로 처리하는지 확인
- 프론트의 일정 생성 플로우와 새 preview/create 응답 간 예외 처리 정리
- 즉흥 여행 API의 실패 코드에 대한 프론트 문구 매핑 정리

## 즉흥 여행 API 실패 코드

프론트/QA에서 바로 참고할 수 있도록,
현재 즉흥 여행 API에서 의미 있게 내려가는 오류 코드를 정리합니다.

### 목적지 추천 API

`POST /api/v1/spontaneous-trips/destinations`

- `DESTINATIONS_NOT_FOUND`
  - 추천 가능한 목적지가 없음
  - 테마/시간/교통 조건이 너무 빡빡한 경우 발생 가능

### 즉흥 코스 API

`POST /api/v1/spontaneous-trips/course`

- `DESTINATION_NOT_FOUND`
  - 전달한 `destinationId` 가 유효하지 않음
- `TRANSPORT_MODE_UNAVAILABLE`
  - 요청한 교통수단으로 출발지 ↔ 목적지 이동이 불가능함
- `PLACE_SEARCH_FAILED`
  - 외부 관광 API 조회 실패
- `COURSE_CANDIDATES_NOT_FOUND`
  - 목적지 주변에서 코스 후보 장소를 찾지 못함
- `COURSE_GENERATION_EMPTY`
  - 후보는 있었지만 코스 조합 결과가 비어 있음
- `COURSE_SEGMENT_ROUTE_UNAVAILABLE`
  - 생성된 코스 내부의 stop 간 이동 경로를 찾지 못함
- `COURSE_RETURN_ROUTE_UNAVAILABLE`
  - 마지막 stop 에서 출발지로 돌아가는 경로를 찾지 못함

프론트 권장 문구 예시:

- `TRANSPORT_MODE_UNAVAILABLE`
  - 선택한 이동수단으로 이동 가능한 코스를 찾지 못했습니다.
- `COURSE_CANDIDATES_NOT_FOUND`
  - 주변에서 추천할 장소를 찾지 못했습니다.
- `COURSE_SEGMENT_ROUTE_UNAVAILABLE`
  - 추천 코스의 이동 경로를 계산하지 못했습니다. 다시 시도해 주세요.
- `COURSE_RETURN_ROUTE_UNAVAILABLE`
  - 복귀 경로를 계산하지 못했습니다. 다른 이동수단으로 다시 시도해 주세요.

## 반드시 알아야 하는 운영 조건

Spring 서버가 FastAPI 일정 API를 사용하려면 아래 env 가 반드시 있어야 합니다.

```env
SCHEDULE_FASTAPI_ENABLED=true
SCHEDULE_FASTAPI_BASE_URL=http://data-ai:8010
```

주의:

- `SCHEDULE_FASTAPI_ENABLED` 가 없으면 일정 API 위임이 비활성화될 수 있음
- `SCHEDULE_FASTAPI_BASE_URL` 은 끝에 슬래시 없이 넣는 것을 권장함
- EC2에서 컨테이너를 수동 재실행할 때 env 누락 시 preview/create 가 실패할 수 있음

FastAPI 컨테이너도 DB / 교통 API 관련 env 가 필요합니다.

예:

```env
SPRING_DATASOURCE_URL=...
SPRING_DATASOURCE_USERNAME=...
SPRING_DATASOURCE_PASSWORD=...
ODSAY_ENABLED=true
ODSAY_API_KEY=...
SKT_API_KEY=...
```

현재 `data/.github/workflows/deploy-dev.yml` 은 위 값을 `.env.dev` 로 만들어
EC2의 `data-ai` 컨테이너를 `--env-file /opt/hackathon-dev/.env.dev` 방식으로 실행하도록 정리된 상태입니다.

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

## 파일 역할 요약

- [app.py](/Users/miju/test_1/data/app.py)
  - FastAPI 진입점
  - health / predict / schedule / spontaneous API 정의
- [schedule/service.py](/Users/miju/test_1/data/schedule/service.py)
  - 일정 생성 핵심 로직
  - 후보 장소 로딩
  - 일정 조합 / stop 구성 / 경로 연결
- [schedule/preview_service.py](/Users/miju/test_1/data/schedule/preview_service.py)
  - 일정 preview 생성 / 조회
- [schedule/persistence.py](/Users/miju/test_1/data/schedule/persistence.py)
  - preview / schedule 저장 관련 처리
- [transit/routing.py](/Users/miju/test_1/data/transit/routing.py)
  - ODSAY / TMAP / 도보 fallback 경로 생성
- [spontaneous/course.py](/Users/miju/test_1/data/spontaneous/course.py)
  - 즉흥 코스 역할별 장소 선택 / 순서 조합
- [spontaneous/routing.py](/Users/miju/test_1/data/spontaneous/routing.py)
  - 즉흥 여행용 교통수단 가능 여부 / 이동시간 계산
- [spontaneous/places.py](/Users/miju/test_1/data/spontaneous/places.py)
  - 즉흥 여행 목적지 주변 장소 조회 / 후보 필터링
- [core/runtime_env.py](/Users/miju/test_1/data/core/runtime_env.py)
  - env 파일 로딩

## 한 줄 상태 요약

현재 `data/` 는 더 이상 단순 분류 서버만이 아니라,
카테고리 분류 + 일정 preview/create/list/get/update/map 을 담당하는 FastAPI 서버로 확장된 상태입니다.

다만 공개 API의 최종 진입점과 일부 운영 흐름은 아직 Spring이 잡고 있으므로,
현재 단계는 “마이그레이션 완료 직전의 하이브리드 운영 상태”로 보면 됩니다.
