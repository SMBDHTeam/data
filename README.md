# AI Server

Standalone Python inference server for the TourAPI category classifier.

It is intentionally kept outside the Spring `server` project so the Java API and
the Python model service can evolve independently.

Model artifacts are stored inside this repository under `data/model_artifacts/`.

## Run

```bash
cd /Users/miju/test_1/data
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8010
```

If `scikit-learn` version warnings appear, reinstall dependencies so the local
environment matches the saved model version.

```bash
cd /Users/miju/test_1/data
source .venv/bin/activate
pip install -r requirements.txt --upgrade
```

## Health Check

```bash
curl -s http://127.0.0.1:8010/health
```

## Predict

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

The response now includes both compatibility fields used by Spring and richer
place-understanding fields for future recommendation logic.

- compatibility: `primary_category`, `theme_answer_id`, `model_category`, `decision_source`
- analysis: `primary_theme`, `secondary_themes`, `semantic_tags`, `mood_tags`,
  `is_meal_place`, `is_low_mobility_friendly`, `cluster_key`, `reason`

## Spring Server And Dev DB

The Python AI server and the Spring server are intentionally separate.

- `data`: model inference only
- `server`: API, DB access, schedule generation

The FastAPI schedule migration now supports a read path for place candidates too.

- First choice: shared PostgreSQL using `SPRING_DATASOURCE_URL`, `SPRING_DATASOURCE_USERNAME`, `SPRING_DATASOURCE_PASSWORD`
- Fallback: local PostgreSQL variables such as `LOCAL_POSTGRES_HOST`, `LOCAL_POSTGRES_PORT`, `LOCAL_POSTGRES_DB`, `LOCAL_POSTGRES_USER`, `LOCAL_POSTGRES_PASSWORD`
- Final fallback: [`candidate_places.json`](/Users/miju/test_1/data/candidate_places.json)

Pointing your local Spring server at the shared development DB is possible, but
it should be done carefully.

- `dev` profile reads `SPRING_DATASOURCE_URL`, `SPRING_DATASOURCE_USERNAME`, and `SPRING_DATASOURCE_PASSWORD`
- `dev` profile also has Flyway enabled, so local startup can try to run DB migrations
- schedule creation and other write APIs can change shared development data

Recommended approach:

1. Keep AI inference in `/data`
2. Use the real deployed development server when you only need to inspect already-ingested data
3. Point local Spring at the shared dev DB only if you have the exact connection info and the team is okay with local access to that shared database
