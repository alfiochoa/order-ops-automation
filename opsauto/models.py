"""Inbound webhook payloads (validated with pydantic) and the YAML config schema."""
from typing import Any, Literal

from pydantic import BaseModel, Field

EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class Address(BaseModel):
    line1: str = Field(min_length=1)
    city: str = Field(min_length=1)
    postal: str = ""
    country: str = Field(min_length=2)


class Item(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=1000)
    price: float = Field(ge=0)


class OrderIn(BaseModel):
    order_id: str = Field(min_length=1)
    email: str = Field(pattern=EMAIL)
    name: str = ""
    currency: str = Field("EUR", min_length=3, max_length=3)
    payment_method: Literal["card", "paypal", "cod", "bank_transfer"]
    items: list[Item] = Field(min_length=1)
    billing: Address
    shipping: Address

    @property
    def total(self) -> float:
        return round(sum(i.qty * i.price for i in self.items), 2)


class LeadIn(BaseModel):
    lead_id: str = Field(min_length=1)
    email: str = Field(pattern=EMAIL)
    name: str = ""
    message: str = ""
    source: str = "web"


class SupportIn(BaseModel):
    ticket_id: str = Field(min_length=1)
    email: str = Field(pattern=EMAIL)
    subject: str = Field(min_length=1)
    body: str = ""


MODELS = {"order": OrderIn, "lead": LeadIn, "support": SupportIn}
REF = {"order": "order_id", "lead": "lead_id", "support": "ticket_id"}


class Cond(BaseModel):
    field: str
    op: Literal["gt", "gte", "lt", "lte", "eq", "ne", "in", "contains"]
    value: Any


class Rule(BaseModel):
    name: str
    kinds: list[str] = ["order"]
    all: list[Cond]
    flag: str
    notify: bool = False


class Retry(BaseModel):
    max_attempts: int = 4
    base_delay: float = 0.5
    factor: float = 2.0
    max_delay: float = 8.0


class Vip(BaseModel):
    min_orders: int = 3
    min_spent: float = 1000


class Risk(BaseModel):
    high_value_threshold: float = 500
    review_threshold: int = 70
    high_risk_countries: list[str] = []
    weights: dict[str, int] = {}


class Config(BaseModel):
    retry: Retry = Retry()
    vip: Vip = Vip()
    risk: Risk = Risk()
    rules: list[Rule] = []
