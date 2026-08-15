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
    desiredThemes: list[str] = []


class TransportOption(BaseModel):
    mode: TransportMode
    available: bool
    outboundMinutes: int | None = None
    returnMinutes: int | None = None
    expectedReturnAt: datetime | None = None
    unavailableReason: str | None = None


class DestinationRecommendation(BaseModel):
    destinationId: str
    name: str
    themeScore: float
    distanceMeters: int
    score: float
    transportOptions: list[TransportOption] = []


class SpontaneousDestinationResponse(BaseModel):
    destinations: list[DestinationRecommendation]