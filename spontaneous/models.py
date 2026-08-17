from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TransportMode(str, Enum):
    PUBLIC_TRANSIT = "PUBLIC_TRANSIT"
    WALK = "WALK"
    BICYCLE = "BICYCLE"
    CAR = "CAR"


class Coordinate(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class SpontaneousDestinationRequest(BaseModel):
    currentLocation: Coordinate
    startAt: datetime
    returnBy: datetime
    desiredThemes: list[str] = Field(default_factory=list)


class TransportOption(BaseModel):
    mode: TransportMode
    available: bool
    outboundMinutes: int | None = None
    returnMinutes: int | None = None
    availableStayMinutes: int | None = None
    expectedReturnAt: datetime | None = None
    unavailableReason: str | None = None


class DestinationRecommendation(BaseModel):
    destinationId: str
    name: str
    themeScore: float
    distanceMeters: int
    score: float
    bestTravelMinutes: int | None = None
    bestStayMinutes: int | None = None
    transportOptions: list[TransportOption] = Field(default_factory=list)


class SpontaneousDestinationResponse(BaseModel):
    destinations: list[DestinationRecommendation]


class SpontaneousCourseStop(BaseModel):
    order: int
    role: str
    name: str
    stayMinutes: int
    themes: list[str] = Field(default_factory=list)
    score: float


class SpontaneousCourseResponse(BaseModel):
    destinationId: str
    name: str
    transportMode: TransportMode
    transport: TransportOption
    course: list[SpontaneousCourseStop] = Field(default_factory=list)


class SpontaneousCourseRequest(BaseModel):
    destinationId: str
    currentLocation: Coordinate
    startAt: datetime
    returnBy: datetime
    desiredThemes: list[str] = Field(default_factory=list)
    transportMode: TransportMode
