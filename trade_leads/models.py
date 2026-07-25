from __future__ import annotations
from typing import Optional
from pydantic import BaseModel


class Review(BaseModel):
    author_name: str = ""
    text: str = ""
    rating: Optional[int] = None


class BusinessDetails(BaseModel):
    place_id: str
    name: str
    address: str = ""
    phone: str = ""
    website: str = ""
    rating: Optional[float] = None
    review_count: int = 0
    business_status: str = ""
    opening_hours: list[str] = []
    reviews: list[Review] = []
    google_maps_url: str = ""


class Lead(BaseModel):
    name: str
    phone: str
    email: str
    website: str
    address: str
    rating: Optional[float]
    review_count: int
    missed_call_score: int
    top_review_excerpts: str  # pipe-separated, up to 3
    has_existing_automation: str  # yes / no / unknown
    personalised_hook: str
    google_maps_url: str
    trade: str = ""
    email_validation_status: str = ""  # valid / invalid / catch-all / spamtrap / abuse / do_not_mail / unknown / ""
