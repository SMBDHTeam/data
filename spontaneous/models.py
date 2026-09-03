from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TransportMode(str, Enum):
    PUBLIC_TRANSIT = "PUBLIC_TRANSIT"
    WALK = "WALK"
    CAR = "CAR"


class TravelTheme(str, Enum):
    SEA = "SEA"
    SEAFOOD = "SEAFOOD"
    FOOD = "FOOD"
    CAFE = "CAFE"
    WALK = "WALK"
    NIGHT_VIEW = "NIGHT_VIEW"
    CULTURE = "CULTURE"
    SHOPPING = "SHOPPING"
    HEALING = "HEALING"
    NATURE = "NATURE"
    ACTIVITY = "ACTIVITY"


class CourseRole(str, Enum):
    ACTIVITY = "ACTIVITY"
    MEAL = "MEAL"
    CAFE = "CAFE"
    NIGHT_VIEW = "NIGHT_VIEW"


class Coordinate(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class SpontaneousDestinationRequest(BaseModel):
    startLocation: Coordinate
    startAt: datetime
    returnBy: datetime
    transportMode: TransportMode
    desiredThemes: list[TravelTheme] = Field(default_factory=list)


class TransportOption(BaseModel):
    mode: TransportMode
    available: bool
    outboundMinutes: int | None = None
    returnMinutes: int | None = None
    availableStayMinutes: int | None = None
    expectedReturnAt: datetime | None = None
    unavailableReason: str | None = None


class TransportSummary(BaseModel):
    mode: TransportMode
    outboundMinutes: int
    returnMinutes: int
    availableStayMinutes: int


class DestinationRecommendation(BaseModel):
    destinationId: str
    name: str
    themeScore: float
    distanceMeters: int
    transport: TransportSummary


class SpontaneousDestinationResponse(BaseModel):
    destinations: list[DestinationRecommendation]


class SpontaneousCourseStop(BaseModel):
    order: int
    role: CourseRole
    name: str
    contentId: str | None = None
    contentTypeId: str | None = None
    latitude: float
    longitude: float
    travelMinutesFromPrevious: int | None = None
    arrivalAt: datetime | None = None
    departureAt: datetime | None = None
    stayMinutes: int
    themes: list[TravelTheme] = Field(default_factory=list)


class SpontaneousCourseResponse(BaseModel):
    destinationId: str
    name: str
    transportMode: TransportMode
    returnTravelMinutes: int | None = None
    estimatedReturnAt: datetime | None = None
    returnBy: datetime | None = None
    course: list[SpontaneousCourseStop] = Field(default_factory=list)


class SpontaneousCourseRequest(BaseModel):
    destinationId: str
    startLocation: Coordinate
    startAt: datetime
    returnBy: datetime
    desiredThemes: list[TravelTheme] = Field(default_factory=list)
    transportMode: TransportMode
