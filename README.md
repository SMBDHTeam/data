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
  - health / predict / schedule API 정의
- [schedule_service.py](/Users/miju/test_1/data/schedule_service.py)
  - 일정 생성 핵심 로직
  - 후보 장소 로딩
  - 일정 조합 / stop 구성 / 경로 연결
- [schedule_preview_service.py](/Users/miju/test_1/data/schedule_preview_service.py)
  - 일정 preview 생성 / 조회
- [schedule_persistence.py](/Users/miju/test_1/data/schedule_persistence.py)
  - preview / schedule 저장 관련 처리
- [transit_routing.py](/Users/miju/test_1/data/transit_routing.py)
  - ODSAY / TMAP / 도보 fallback 경로 생성
- [runtime_env.py](/Users/miju/test_1/data/runtime_env.py)
  - env 파일 로딩

## 한 줄 상태 요약

현재 `data/` 는 더 이상 단순 분류 서버만이 아니라,
카테고리 분류 + 일정 preview/create/list/get/update/map 을 담당하는 FastAPI 서버로 확장된 상태입니다.

다만 공개 API의 최종 진입점과 일부 운영 흐름은 아직 Spring이 잡고 있으므로,
현재 단계는 “마이그레이션 완료 직전의 하이브리드 운영 상태”로 보면 됩니다.


## 배포에 필요한 GitHub Secrets

`deploy-dev.yml`은 아래 값을 요구합니다. 하나라도 비어 있으면 배포가 실패합니다.

| Secret | 용도 |
| --- | --- |
| `DEV_HOST`, `DEV_SSH_USER`, `DEV_SSH_KEY` | EC2 접속 |
| `AWS_REGION`, `AWS_ROLE_ARN`, `ECR_REPOSITORY_NAME` | 이미지 push |
| `SPRING_DATASOURCE_URL` | 일정·후보 장소 DB |
| `SPRING_DATASOURCE_USERNAME` | 〃 |
| `SPRING_DATASOURCE_PASSWORD` | 〃 |
| `ODSAY_API_KEY` | 실제 대중교통 경로 |
| `SKT_API_KEY` | TMAP 보행 경로 (선택. 없으면 보행 경로 비활성) |

`server` 레포와 같은 값을 쓰므로 조직(Organization) Secret으로 한 번만 등록하는 편이 낫습니다.
두 레포 모두 public이라 무료 플랜에서도 조직 Secret을 쓸 수 있습니다.

### DB env가 없으면 조용히 망가진다

`SPRING_DATASOURCE_*`가 없으면 `db_enabled()`가 `False`가 되어 이렇게 동작합니다.

- 일정이 **메모리에만** 저장된다. 컨테이너를 재시작하면 전부 사라진다.
- 후보 장소가 DB(수백 곳)가 아니라 JSON 폴백(15곳)으로 줄어 일정 품질이 급락한다.
- 그럼에도 API는 계속 `200`/`201`을 반환한다.

`/health`의 `schedule_candidate_source`가 `database`인지로 확인할 수 있다.

```bash
curl -s http://127.0.0.1:8010/health
```

`json_fallback` 또는 `built_in_default`가 보이면 DB env가 빠진 상태입니다.

## 배포 환경변수 파일

이 워크플로는 EC2의 `/opt/hackathon-dev/.env.data`에 환경변수를 기록하고
컨테이너에 `--env-file`로 전달합니다.

`server` 레포는 같은 디렉터리의 `.env.server`를 씁니다. 예전에는 두 레포가 모두
`.env.dev`를 써서 나중에 배포한 쪽이 상대의 값을 덮어썼고, 그 뒤 상대 컨테이너가
재시작하면 설정을 잃었습니다. 파일 분리로 이 간섭을 없앴습니다.
