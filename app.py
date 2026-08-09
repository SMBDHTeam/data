from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

try:
    from runtime_env import load_runtime_env
except ModuleNotFoundError:  # pragma: no cover
    from data.runtime_env import load_runtime_env

load_runtime_env()

try:
    from place_analysis import PlaceAnalysis, analyze_place
    from schedule_models import (
        ScheduleCreateRequest,
        ScheduleListResponse,
        ScheduleMapResponse,
        SchedulePreviewCreateRequest,
        SchedulePreviewResponse,
        SchedulePreviewScheduleRequest,
        ScheduleResponse,
        ScheduleUpdateRequest,
    )
    from schedule_preview_service import (
        attach_schedule_to_preview,
        consume_preview,
        create_preview,
        get_preview,
    )
    from schedule_service import (
        CANDIDATE_POOL,
        CANDIDATE_POOL_SOURCE,
        create_schedule,
        get_schedule,
        get_schedule_map,
        list_schedules,
        update_schedule,
    )
except ModuleNotFoundError:  # pragma: no cover
    from data.place_analysis import PlaceAnalysis, analyze_place
    from data.schedule_models import (
        ScheduleCreateRequest,
        ScheduleListResponse,
        ScheduleMapResponse,
        SchedulePreviewCreateRequest,
        SchedulePreviewResponse,
        SchedulePreviewScheduleRequest,
        ScheduleResponse,
        ScheduleUpdateRequest,
    )
    from data.schedule_preview_service import (
        attach_schedule_to_preview,
        consume_preview,
        create_preview,
        get_preview,
    )
    from data.schedule_service import (
        CANDIDATE_POOL,
        CANDIDATE_POOL_SOURCE,
        create_schedule,
        get_schedule,
        get_schedule_map,
        list_schedules,
        update_schedule,
    )

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "model_artifacts" / "tourapi_category_classifier_linear_svc.joblib"

SELECTED_NUMERIC_COLS = [
    "mapx",
    "mapy",
    "title_len",
    "overview_len",
    "has_image",
    "detail_image_count",
    "has_parking_info",
    "has_restdate_info",
    "has_usetime_info",
    "pet_friendly_flag",
]

THEME_TO_ANSWER_ID = {
    "FOOD": "THEME_FOOD",
    "NATURE": "THEME_NATURE",
    "CULTURE": "THEME_CULTURE",
    "ACTIVITY": "THEME_ACTIVITY",
    "SHOPPING": "THEME_SHOPPING",
    "HEALING": "THEME_HEALING",
}

CONTENT_TYPE_CATEGORY_RULES = {
    "39": "FOOD",
    "38": "SHOPPING",
    "28": "ACTIVITY",
    "14": "CULTURE",
    "32": "HEALING",
}

CAT3_PREFIX_CATEGORY_RULES = (
    ("A05", "FOOD"),
    ("A04", "SHOPPING"),
    ("A03", "ACTIVITY"),
    ("A02", "CULTURE"),
    ("A01", "NATURE"),
    ("B02", "HEALING"),
)

KEYWORD_CATEGORY_RULES = (
    ("FOOD", (
        "맛집", "식당", "카페", "브런치", "디저트", "국밥", "밀면", "갈비",
        "횟집", "해산물", "커피", "베이커리", "푸드", "음식"
    )),
    ("SHOPPING", (
        "시장", "쇼핑", "백화점", "아울렛", "몰", "상가", "기념품", "면세점"
    )),
    ("ACTIVITY", (
        "체험", "액티비티", "케이블카", "루지", "요트", "서핑", "sup",
        "패들보드", "카약", "짚라인", "레일바이크", "테마파크"
    )),
    ("CULTURE", (
        "박물관", "역사관", "미술관", "전시", "사찰", "절", "문화", "유적",
        "기념관", "서원", "고택", "전통", "마을"
    )),
    ("HEALING", (
        "온천", "스파", "사우나", "리조트", "풀빌라", "휴양", "힐링", "휴식", "조용한"
    )),
    ("NATURE", (
        "해수욕장", "해변", "바다", "공원", "산책로", "숲", "수목원", "섬",
        "해안", "절경", "전망대", "강", "호수", "산", "동백", "태종대"
    )),
)


class PredictRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    title: str = Field(default="")
    overview: str | None = None
    addr1: str | None = None
    contenttypeid: str | None = None
    firstimage: str | None = None
    detail_image_count: float | int | None = 0
    parking: str | None = None
    restdate: str | None = None
    usetime: str | None = None
    chkpet: str | None = None
    lDongSignguCd: str | int | None = None
    lDongRegnCd: str | int | None = None
    cat1: str | None = None
    cat2: str | None = None
    cat3: str | None = None
    lclsSystm1: str | None = None
    lclsSystm2: str | None = None
    lclsSystm3: str | None = None
    mapx: float | None = None
    mapy: float | None = None


class PredictResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    primary_category: str
    theme_answer_id: str | None
    model_category: str | None
    decision_source: str
    primary_theme: str
    secondary_themes: list[str]
    semantic_tags: list[str]
    mood_tags: list[str]
    is_meal_place: bool
    is_low_mobility_friendly: bool
    cluster_key: str
    reason: str
    probabilities: dict[str, float] | None
    features: dict[str, Any]


def get_series(frame: pd.DataFrame, column_name: str) -> pd.Series:
    if column_name in frame.columns:
        return frame[column_name]
    return pd.Series([pd.NA] * len(frame), index=frame.index)


def build_text(frame: pd.DataFrame) -> pd.Series:
    return (
        get_series(frame, "title").fillna("").astype(str).str.strip() + " "
        + get_series(frame, "overview").fillna("").astype(str).str.strip() + " "
        + get_series(frame, "addr1").fillna("").astype(str).str.strip()
    ).str.replace(r"\s+", " ", regex=True).str.strip()


def source_category(frame: pd.DataFrame) -> pd.Series:
    return (
        get_series(frame, "cat3")
        .fillna(get_series(frame, "cat2"))
        .fillna(get_series(frame, "cat1"))
        .fillna(get_series(frame, "lclsSystm3"))
        .fillna(get_series(frame, "lclsSystm2"))
        .fillna(get_series(frame, "lclsSystm1"))
        .fillna(get_series(frame, "contenttypeid"))
    )


def build_features(payloads: list[PredictRequest]) -> pd.DataFrame:
    frame = pd.DataFrame([payload.model_dump() for payload in payloads])

    for col in ["mapx", "mapy", "lDongSignguCd", "lDongRegnCd"]:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame["text"] = build_text(frame)
    frame["title_len"] = get_series(frame, "title").fillna("").astype(str).str.len()
    frame["overview_len"] = get_series(frame, "overview").fillna("").astype(str).str.len()
    frame["text_len"] = frame["text"].fillna("").astype(str).str.len()
    frame["has_image"] = get_series(frame, "firstimage").notna().astype(int)
    frame["detail_image_count"] = pd.to_numeric(
        get_series(frame, "detail_image_count"), errors="coerce"
    ).fillna(0)
    frame["has_parking_info"] = get_series(frame, "parking").notna().astype(int)
    frame["has_restdate_info"] = get_series(frame, "restdate").notna().astype(int)
    frame["has_usetime_info"] = get_series(frame, "usetime").notna().astype(int)
    frame["pet_friendly_flag"] = (
        get_series(frame, "chkpet")
        .fillna("")
        .astype(str)
        .str.contains("가능|허용|동반", regex=True)
        .astype(int)
    )
    district_numeric = pd.to_numeric(get_series(frame, "lDongSignguCd"), errors="coerce").fillna(-1).astype(int)
    frame["district_str"] = district_numeric.astype(str)
    frame["source_category"] = source_category(frame)

    model_df = frame[
        [
            "text",
            *SELECTED_NUMERIC_COLS,
            "district_str",
            "source_category",
        ]
    ].copy()
    return model_df


def load_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Model file not found: {MODEL_PATH}")
    return joblib.load(MODEL_PATH)


def source_category_from_payload(payload: PredictRequest) -> str:
    return (
        payload.cat3
        or payload.cat2
        or payload.cat1
        or payload.lclsSystm3
        or payload.lclsSystm2
        or payload.lclsSystm1
        or payload.contenttypeid
        or ""
    ).strip().upper()


def normalize_text(*values: str | None) -> str:
    return " ".join(value.strip() for value in values if value and value.strip()).lower()


def keyword_category(text: str) -> str | None:
    for category, keywords in KEYWORD_CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return None


def rule_based_category(payload: PredictRequest) -> tuple[str | None, str | None]:
    text = normalize_text(payload.title, payload.overview, payload.addr1)
    if any(keyword in text for keyword in ("자갈치", "수산시장", "어시장", "회센터", "수산", "해산물시장")):
        return "FOOD", "food_market_keyword"

    content_type_id = (payload.contenttypeid or "").strip()
    if content_type_id in CONTENT_TYPE_CATEGORY_RULES:
        return CONTENT_TYPE_CATEGORY_RULES[content_type_id], "contenttypeid"

    source_category = source_category_from_payload(payload)
    for prefix, category in CAT3_PREFIX_CATEGORY_RULES:
        if source_category.startswith(prefix):
            return category, "cat3_prefix"

    keyword_match = keyword_category(text)
    if keyword_match is not None:
        return keyword_match, "keyword"
    return None, None


def known_source_categories() -> set[str]:
    if not hasattr(model, "named_steps"):
        return set()
    preprocessor = model.named_steps.get("preprocessor")
    if preprocessor is None or not hasattr(preprocessor, "transformers_"):
        return set()
    cat_transformer = next(
        (transformer for name, transformer, _ in preprocessor.transformers_ if name == "cat"),
        None,
    )
    if cat_transformer is None or not hasattr(cat_transformer, "named_steps"):
        return set()
    onehot = cat_transformer.named_steps.get("onehot")
    if onehot is None or not hasattr(onehot, "categories_") or len(onehot.categories_) < 2:
        return set()
    return {str(category) for category in onehot.categories_[1]}


def conservative_keyword_override(payload: PredictRequest, model_prediction: str) -> tuple[str | None, str | None]:
    source_category = source_category_from_payload(payload)
    if not source_category or source_category in MODEL_SOURCE_CATEGORIES:
        return None, None

    keyword_match = keyword_category(normalize_text(payload.title, payload.overview, payload.addr1))
    if keyword_match is None or keyword_match == model_prediction:
        return None, None

    content_type_id = (payload.contenttypeid or "").strip()
    if content_type_id == "12" and keyword_match == "NATURE":
        return "NATURE", "keyword_unknown_source_tourism_override"

    if model_prediction in {"FOOD", "SHOPPING"} and keyword_match in {"NATURE", "CULTURE", "ACTIVITY", "HEALING"}:
        return keyword_match, "keyword_unknown_source_override"

    return None, None


def resolve_primary_category(payload: PredictRequest, model_prediction: str) -> tuple[str, str]:
    category, source = rule_based_category(payload)
    if category is not None:
        return category, source or "rule"
    conservative_override, override_source = conservative_keyword_override(payload, model_prediction)
    if conservative_override is not None:
        return conservative_override, override_source or "keyword_override"
    return model_prediction, "model"


def response_from_prediction(
    payload: PredictRequest,
    prediction: str,
    model_prediction: str,
    decision_source: str,
    features: pd.DataFrame,
    index: int,
    probabilities: dict[str, float] | None,
) -> PredictResponse:
    analysis: PlaceAnalysis = analyze_place(
        prediction,
        normalize_text(payload.title, payload.overview, payload.addr1),
        payload.contenttypeid,
        payload.cat3 or payload.cat2 or payload.cat1 or payload.lclsSystm3 or payload.lclsSystm2 or payload.lclsSystm1,
    )
    return PredictResponse(
        primary_category=prediction,
        theme_answer_id=THEME_TO_ANSWER_ID.get(prediction),
        model_category=model_prediction,
        decision_source=decision_source,
        primary_theme=analysis.primary_theme,
        secondary_themes=analysis.secondary_themes,
        semantic_tags=analysis.semantic_tags,
        mood_tags=analysis.mood_tags,
        is_meal_place=analysis.is_meal_place,
        is_low_mobility_friendly=analysis.is_low_mobility_friendly,
        cluster_key=analysis.cluster_key,
        reason=analysis.reason,
        probabilities=probabilities,
        features=features.iloc[index].replace({pd.NA: None, np.nan: None}).to_dict(),
    )


app = FastAPI(title="TourAPI AI Server", version="0.1.0")
model = load_model()
MODEL_SOURCE_CATEGORIES = known_source_categories()


@app.get("/health")
def health() -> dict[str, str | int]:
    return {
        "status": "ok",
        "model_path": str(MODEL_PATH),
        "schedule_candidate_source": CANDIDATE_POOL_SOURCE,
        "schedule_candidate_count": len(CANDIDATE_POOL),
    }


@app.post("/predict", response_model=PredictResponse)
def predict(payload: PredictRequest) -> PredictResponse:
    try:
        features = build_features([payload])
        model_prediction = str(model.predict(features)[0])
        prediction, decision_source = resolve_primary_category(payload, model_prediction)
        probabilities = None
        estimator = model.named_steps.get("model")
        if hasattr(estimator, "decision_function"):
            scores = estimator.decision_function(model.named_steps["preprocessor"].transform(features))
            if np.ndim(scores) == 1:
                scores = np.vstack([-scores, scores]).T
            classes = estimator.classes_
            row = scores[0]
            exp = np.exp(row - np.max(row))
            normalized = exp / exp.sum()
            probabilities = {
                str(label): round(float(score), 6)
                for label, score in zip(classes, normalized, strict=False)
            }

        return response_from_prediction(
            payload,
            prediction,
            model_prediction,
            decision_source,
            features,
            0,
            probabilities,
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/predict/batch")
def predict_batch(payloads: list[PredictRequest]) -> list[PredictResponse]:
    if not payloads:
        raise HTTPException(status_code=400, detail="At least one payload is required")
    try:
        features = build_features(payloads)
        model_predictions = model.predict(features)
        responses: list[PredictResponse] = []
        for index, model_prediction in enumerate(model_predictions):
            resolved_prediction, decision_source = resolve_primary_category(
                payloads[index], str(model_prediction)
            )
            responses.append(
                response_from_prediction(
                    payloads[index],
                    resolved_prediction,
                    str(model_prediction),
                    decision_source,
                    features,
                    index,
                    None,
                )
            )
        return responses
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/schedule-previews", response_model=SchedulePreviewResponse, status_code=201)
def create_schedule_preview_endpoint(payload: SchedulePreviewCreateRequest) -> SchedulePreviewResponse:
    return create_preview(payload)


@app.get("/api/v1/schedule-previews/{preview_id}", response_model=SchedulePreviewResponse)
def get_schedule_preview_endpoint(preview_id: UUID) -> SchedulePreviewResponse:
    return get_preview(preview_id)


@app.post("/api/v1/schedules", response_model=ScheduleResponse, status_code=201)
def create_schedule_endpoint(
    payload: ScheduleCreateRequest | SchedulePreviewScheduleRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScheduleResponse:
    if idempotency_key:
        if not isinstance(payload, SchedulePreviewScheduleRequest):
            payload = SchedulePreviewScheduleRequest.model_validate(payload.model_dump(by_alias=True))
        preview, create_request, fixed_events_by_day = consume_preview(payload)
        schedule = create_schedule(
            create_request,
            preview_id=preview.preview_id,
            fixed_events_by_day=fixed_events_by_day,
        )
        attach_schedule_to_preview(preview.preview_id, schedule.id)
        return schedule

    if not isinstance(payload, ScheduleCreateRequest):
        payload = ScheduleCreateRequest.model_validate(payload.model_dump(by_alias=True))
    return create_schedule(payload)


@app.get("/api/v1/schedules", response_model=ScheduleListResponse)
def list_schedules_endpoint() -> ScheduleListResponse:
    return list_schedules()


@app.get("/api/v1/schedules/{schedule_id}", response_model=ScheduleResponse)
def get_schedule_endpoint(schedule_id: UUID) -> ScheduleResponse:
    return get_schedule(schedule_id)


@app.patch("/api/v1/schedules/{schedule_id}", response_model=ScheduleResponse)
def update_schedule_endpoint(schedule_id: UUID, payload: ScheduleUpdateRequest) -> ScheduleResponse:
    return update_schedule(schedule_id, payload)


@app.get("/api/v1/schedules/{schedule_id}/map", response_model=ScheduleMapResponse)
def get_schedule_map_endpoint(schedule_id: UUID, dayNo: int | None = None) -> ScheduleMapResponse:
    return get_schedule_map(schedule_id, dayNo)
