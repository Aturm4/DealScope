"""Validated values shared by parsing, filtering and persistent storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from urllib.parse import urlsplit

MAX_TEXT_LENGTH = 32_000
MAX_ITEMS = 100
MAX_PRICE = Decimal("1000000000000")
CURRENCIES = {"USD", "UZS"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Дата должна содержать часовой пояс.")
    return value.astimezone(UTC).isoformat()


def read_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError("Некорректная дата.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        timestamp(result)
        return result.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Ожидается дата ISO 8601 с часовым поясом.") from exc


def money(value: str | Decimal) -> Decimal:
    if not isinstance(value, (str, Decimal)) or len(str(value)) > 32:
        raise ValueError("Цена должна быть десятичной строкой.")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Некорректная цена.") from exc
    if not result.is_finite() or result < 0 or result > MAX_PRICE:
        raise ValueError("Цена вне допустимого диапазона.")
    if result.as_tuple().exponent < -2:
        raise ValueError("Цена должна содержать не более двух десятичных знаков.")
    return result


def checked_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,79}", value):
        raise ValueError("Идентификатор: 1–80 латинских букв, цифр, двоеточий, _ или -.")
    return value


def checked_url(value: str | None) -> str | None:
    """Validate a stored reference; this function never resolves or fetches URLs."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Некорректная ссылка на оригинал.")
    if any(ord(char) < 33 or char == "\\" for char in value):
        raise ValueError("Недопустимые символы в ссылке.")
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError
        if parts.port not in (None, 443):
            raise ValueError
    except ValueError as exc:
        raise ValueError("Ссылка должна быть HTTPS, без учётных данных и нестандартного порта.") from exc
    return value


@dataclass(frozen=True)
class IncomingPost:
    message_id: int
    text: str
    published_at: datetime
    original_url: str | None = None
    edited_at: datetime | None = None
    has_media: bool = False

    def __post_init__(self) -> None:
        if type(self.message_id) is not int or not 0 < self.message_id < 2**63:
            raise ValueError("ID публикации должен быть положительным 64-битным числом.")
        if not isinstance(self.text, str) or len(self.text) > MAX_TEXT_LENGTH:
            raise ValueError(f"Текст должен содержать не более {MAX_TEXT_LENGTH} символов.")
        timestamp(self.published_at)
        if self.edited_at is not None:
            timestamp(self.edited_at)
            if self.edited_at < self.published_at:
                raise ValueError("Редактирование не может предшествовать публикации.")
        if type(self.has_media) is not bool:
            raise ValueError("has_media должен быть логическим значением.")
        checked_url(self.original_url)

    @property
    def version_time(self) -> datetime:
        return (self.edited_at or self.published_at).astimezone(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "text": self.text,
            "published_at": timestamp(self.published_at),
            "original_url": self.original_url,
            "edited_at": timestamp(self.edited_at) if self.edited_at else None,
            "has_media": self.has_media,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IncomingPost:
        if not isinstance(data, dict):
            raise ValueError("Публикация должна быть объектом JSON.")
        permitted = {"message_id", "text", "published_at", "original_url", "edited_at", "has_media"}
        if set(data) - permitted or not {"message_id", "text", "published_at"} <= set(data):
            raise ValueError("Неизвестные или отсутствующие поля публикации.")
        return cls(
            message_id=data["message_id"],
            text=data["text"],
            published_at=read_timestamp(data["published_at"]),
            original_url=data.get("original_url"),
            edited_at=read_timestamp(data["edited_at"]) if data.get("edited_at") else None,
            has_media=data.get("has_media", False),
        )


@dataclass(frozen=True)
class Offer:
    ordinal: int
    text: str
    search_text: str
    drive_type: str | None = None
    brand: str | None = None
    model: str | None = None
    capacity_gb: int | None = None
    capacity_raw: str | None = None
    interface: str | None = None
    form_factor: str | None = None
    condition: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    price_kind: str = "unknown"
    availability: str = "unmarked"
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["price"] = str(self.price) if self.price is not None else None
        result["issues"] = list(self.issues)
        return result


@dataclass(frozen=True)
class ParsedPost:
    status: str
    offers: tuple[Offer, ...] = ()
    issues: tuple[str, ...] = ()


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise ValueError(f"{field_name}: ожидается список, не более 32 значений.")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in value):
        raise ValueError(f"{field_name}: значения должны быть непустыми строками до 100 символов.")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class SearchRule:
    key: str
    name: str
    currency: str
    max_price: Decimal
    capacities_gb: tuple[int, ...] = (512, 1000)
    drive_type: str | None = "ssd"
    interface: str | None = "nvme"
    brands: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    keyword_mode: str = "any"
    exclusions: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        checked_key(self.key)
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 100:
            raise ValueError("Укажите название правила до 100 символов.")
        if self.currency not in CURRENCIES:
            raise ValueError("Поддерживаются валюты USD и UZS, без автоматического пересчёта.")
        object.__setattr__(self, "max_price", money(self.max_price))
        if not isinstance(self.capacities_gb, (list, tuple)) or len(self.capacities_gb) > 16:
            raise ValueError("Некорректный список объёмов.")
        if any(type(cap) is not int or not 0 < cap <= 100_000 for cap in self.capacities_gb):
            raise ValueError("Объёмы задаются целыми положительными числами в GB.")
        object.__setattr__(self, "capacities_gb", tuple(self.capacities_gb))
        if self.drive_type not in (None, "ssd", "hdd") or self.interface not in (None, "nvme", "sata"):
            raise ValueError("Некорректный тип накопителя или интерфейс.")
        for field_name in ("brands", "models", "conditions", "keywords", "exclusions"):
            object.__setattr__(self, field_name, _strings(getattr(self, field_name), field_name))
        if any(condition not in ("new", "used") for condition in self.conditions):
            raise ValueError("Состояние: new или used.")
        if self.keyword_mode not in ("any", "all") or type(self.enabled) is not bool:
            raise ValueError("Некорректный режим ключевых слов или enabled.")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["max_price"] = str(self.max_price)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchRule:
        if not isinstance(data, dict):
            raise ValueError("Правило должно быть объектом JSON.")
        fields = set(cls.__dataclass_fields__)
        if set(data) - fields or not {"key", "name", "currency", "max_price"} <= set(data):
            raise ValueError("Неизвестные или отсутствующие поля правила.")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ValueError("Некорректные значения правила.") from exc


@dataclass(frozen=True)
class Decision:
    status: str
    reasons: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessResult:
    status: str
    post_id: int
    revision: int
    matched: int = 0
    needs_review: int = 0
