"""Conservative text-only extraction. No inferred market prices, OCR or model calls."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from .models import MAX_ITEMS, MAX_PRICE, MAX_TEXT_LENGTH, Offer, ParsedPost

PARSER_VERSION = "1"


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    return value.translate(str.maketrans({"‘": "'", "’": "'", "ʻ": "'", "ʼ": "'", "`": "'"}))


_CAPACITY = re.compile(r"(?<![\d.,])(?P<amount>\d{1,5}(?:[.,]\d{1,3})?)\s*(?P<unit>tb|тб|gb|гб)(?![a-zа-я])")
_CURRENCY = r"(?:usd|uzs|so'?m|с[уў]м(?:ов)?|доллар(?:ов|а)?|\$)"
_NUMBER = r"(?:\d{1,3}(?:[ .,']\d{3}){1,4}(?:[.,]\d{1,2})?|\d{1,13}(?:[.,]\d{1,2})?)"
_PRICE = re.compile(
    rf"(?P<before>{_CURRENCY})\s*(?P<number_before>{_NUMBER})(?![\d.,])"
    rf"|(?<![\d.,])(?P<number_after>{_NUMBER})\s*(?P<after>{_CURRENCY})(?![a-zа-я])"
)
_NO_CURRENCY_PRICE = re.compile(rf"(?:цена|стоимость|narx)\s*[:=\-]?\s*({_NUMBER})(?![\d.,])")
_CROSS = re.compile("[❌❎✖✕✘]")
_SOLD = re.compile(r"\b(?:продан(?:о|а|ы)?|sotildi|sold)\b")
_NOT_SOLD = re.compile(r"\b(?:не\s+(?:вс[её]\s+)?продан(?:о|а|ы)?|not\s+sold)\b")
_GLOBAL_SOLD = re.compile(r"^\W*(?:(?:вс[её]|все\s+позиции)\s+)?(?:продан(?:о|а|ы)?|sotildi|sold)(?:\W|sotildi|sold|продано)*$")
_SERVICE = re.compile(r"достав|обмен|гарант|рассроч|предоплат|телефон|контакт")
_BRANDS = {
    "western digital": "Western Digital", "wd": "Western Digital", "kingfast": "Kingfast",
    "kingston": "Kingston", "biostar": "Biostar", "axle": "Axle", "hikvision": "Hikvision",
    "samsung": "Samsung", "crucial": "Crucial", "adata": "ADATA", "a-data": "ADATA",
    "xpg": "XPG", "sandisk": "SanDisk", "sk hynix": "SK hynix", "hynix": "SK hynix",
    "kingspec": "KingSpec", "teamgroup": "TeamGroup", "patriot": "Patriot",
    "transcend": "Transcend", "intel": "Intel", "lexar": "Lexar", "netac": "Netac",
    "silicon power": "Silicon Power", "apacer": "Apacer", "toshiba": "Toshiba",
    "seagate": "Seagate", "pny": "PNY",
}
_BRAND = re.compile(r"(?<![a-zа-я])(?:" + "|".join(re.escape(b) for b in sorted(_BRANDS, key=len, reverse=True)) + r")(?![a-zа-я])")
_SSD = re.compile(r"\b(?:ssd|ссд|сsd)\b")
_HDD = re.compile(r"\bhdd\b|ж[её]стк(?:ий|ие)\s+диск")
_OTHER = re.compile(r"\b(?:ddr[345]|rtx|gtx)\b|видеокарт|материнск|процессор|оперативн")
_ACCESSORY = re.compile(r"(?m)^\W*(?:продам\s+)?(?:адаптер|переходник|бокс|радиатор|кабель|корпус)\b")
_NVME = re.compile(r"\bnvme\b|\bnvm\s+express\b")
_SATA = re.compile(r"\bsata\b")
_M2 = re.compile(r"\bm[.\s-]?2\b")
_NEW = re.compile(r"\b(?:новый|новая|новое|новые|new|yangi)\b")
_USED = re.compile(r"(?<![а-яa-z])б\s*(?:[/\\.\-]\s*)?у(?![а-яa-z])|\b(?:used|ishlatilgan)\b")
_BUY = re.compile(r"\b(?:куплю|скупка|ищу|sotib\s+olaman)\b")


def _currency(raw: str) -> str:
    return "USD" if raw == "$" or raw.startswith(("usd", "доллар")) else "UZS"


def _amount(raw: str) -> Decimal | None:
    raw = raw.replace(" ", "").replace("'", "")
    if "." in raw and "," in raw:
        decimal_mark = "." if raw.rfind(".") > raw.rfind(",") else ","
        raw = raw.replace("," if decimal_mark == "." else ".", "").replace(decimal_mark, ".")
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        raw = raw.replace(".", "").replace(",", "")
    else:
        raw = raw.replace(",", ".")
    try:
        value = Decimal(raw)
        return value if value.is_finite() and 0 <= value <= MAX_PRICE else None
    except InvalidOperation:
        return None


def extract_capacities(text: str) -> tuple[tuple[int, str], ...]:
    if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH:
        raise ValueError("Текст превышает допустимую длину.")
    found: list[tuple[int, str]] = []
    for match in _CAPACITY.finditer(normalize(text)):
        number = Decimal(match["amount"].replace(",", "."))
        gb = number * (1000 if match["unit"] in ("tb", "тб") else 1)
        if 0 < gb <= 100_000 and gb == gb.to_integral_value():
            item = (int(gb), match[0])
            if item not in found:
                found.append(item)
    return tuple(found)


def extract_price(text: str) -> tuple[Decimal | None, str | None, str, tuple[str, ...]]:
    if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH:
        raise ValueError("Текст превышает допустимую длину.")
    # Collapse whitespace once, then inspect contexts of bounded price tokens.
    # Never scan the whole post with adjacent, overlapping unbounded repetitions.
    value = " ".join(normalize(text).split())
    found: list[tuple[Decimal, str]] = []
    matches: list[re.Match[str]] = []
    for match in _PRICE.finditer(value):
        matches.append(match)
        if len(matches) > 200:
            return None, None, "ambiguous", ("too_many_price_tokens",)
    malformed = False
    price_range = starting_price = False
    for match in matches:
        raw_number = match["number_before"] or match["number_after"]
        number = _amount(raw_number)
        start = match.start("number_before" if match["number_before"] else "number_after")
        previous = start - 1
        while previous >= 0 and value[previous] in " .,':":
            previous -= 1
        if previous >= 0 and value[previous].isdecimal():
            malformed = True
        prefix = value[:match.start()].rstrip()
        suffix = value[match.end():].lstrip()
        if prefix.endswith(("-", "−")) or value[:start].rstrip().endswith(("-", "−")):
            malformed = True
        if prefix and prefix[-1] in "-–—/":
            left = prefix[:-1].rstrip()
            price_range |= bool(left and left[-1].isdecimal())
        if suffix and suffix[0] in "-–—/":
            right = suffix[1:].lstrip()
            price_range |= bool(right and right[0].isdecimal())
        starting_price |= bool(re.search(r"\b(?:от|from)$", prefix[-16:]))
        starting_price |= bool(re.match(r"(?:dan\b|и\s+выше\b)", suffix[:20]))
        if number is None:
            malformed = True
        else:
            pair = (number, _currency(match["before"] or match["after"]))
            if pair not in found:
                found.append(pair)
    if malformed:
        return None, None, "ambiguous", ("malformed_price",)
    if price_range:
        return None, None, "ambiguous", ("price_range",)
    if len(found) > 1:
        return None, None, "ambiguous", ("multiple_prices",)
    if not found:
        unqualified = _NO_CURRENCY_PRICE.search(value)
        return (_amount(unqualified[1]) if unqualified else None, None, "unknown", ())
    amount, currency = found[0]
    if re.search(r"предоплат|аванс|\bdeposit\b|boshlang'ich", value):
        return amount, currency, "deposit", ("deposit",)
    if re.search(r"рассроч|ежемесяч|в\s+месяц|/\s*мес|\boyiga\b|bo'lib", value):
        return amount, currency, "installment", ("installment",)
    if starting_price:
        return amount, currency, "from", ("starting_price",)
    return amount, currency, "full", ()


def _brand_model(text: str) -> tuple[str | None, str | None]:
    value = normalize(text)
    match = _BRAND.search(value)
    brand = _BRANDS[match[0]] if match else None
    explicit = re.search(r"(?:модель|model)\s*:\s*([^\n,;]+)", value)
    tail = explicit[1] if explicit else value[match.end():].split("\n", 1)[0] if match else ""
    if tail:
        stops = [m.start() for pattern in (_CAPACITY, _PRICE, _NVME, _SATA, _M2, _NEW, _USED) if (m := pattern.search(tail))]
        tail = tail[:min(stops)] if stops else tail
        tail = tail.strip(" :;,-🟠•")
        tokens = tail.split()
        if len(tokens) > 6:
            return brand, None
        # Model numbers must actually be written, never inferred from capacity/brand.
        if tokens and any(re.search(r"[a-z]", token) and re.search(r"\d", token) for token in tokens):
            return brand, " ".join(tokens).upper()
        if tokens and any(token.isdecimal() for token in tokens):
            return brand, " ".join(tokens).upper()
    return brand, None


def _sold(text: str) -> tuple[bool, bool]:
    value = normalize(text)
    negated = bool(_NOT_SOLD.search(value))
    without_negation = _NOT_SOLD.sub("", value)
    positive = bool(_SOLD.search(without_negation))
    cross = False
    for line in value.splitlines():
        if _CROSS.search(line):
            product_context = _CAPACITY.search(line) or _PRICE.search(line) or _BRAND.search(line)
            if product_context or (not _SERVICE.search(line) and not re.sub(r"[\W_]", "", line)):
                cross = True
    if negated and cross and not positive:
        return False, True
    return positive or cross, False


def _extract(text: str, header: str, ordinal: int, global_sold: bool, ambiguous: bool) -> Offer:
    combined = "\n".join(part for part in (header, text) if part)
    value = normalize(combined)
    issues: list[str] = ["ambiguous_product_price"] if ambiguous else []
    if len({_BRANDS[match[0]] for match in _BRAND.finditer(normalize(text))}) > 1:
        issues.append("multiple_products_in_block")
    capacities = extract_capacities(combined)
    unique_caps = {cap for cap, _ in capacities}
    if len(unique_caps) > 1:
        issues.append("multiple_capacities")
    capacity = next(iter(unique_caps)) if len(unique_caps) == 1 else None
    price, currency, kind, price_issues = extract_price(text)
    issues.extend(price_issues)
    ssd, hdd = bool(_SSD.search(value)), bool(_HDD.search(value))
    drive_type = "ssd" if ssd and not hdd else "hdd" if hdd and not ssd else None
    if _OTHER.search(value) or _ACCESSORY.search(value):
        drive_type = "other"
    if ssd and hdd:
        issues.append("conflicting_drive_types")
    interface_text = re.sub(r"\b(?:не|not)\s+(?:nvme|sata)\b", "", value)
    interface_text = re.sub(r"(?m)^\W*[❌❎✖✕✘]\ufe0f?\s*(?:nvme|sata)\b", "", interface_text)
    nvme, sata = bool(_NVME.search(interface_text)), bool(_SATA.search(interface_text))
    interface = "nvme" if nvme and not sata else "sata" if sata and not nvme else None
    if nvme and sata:
        issues.append("conflicting_interfaces")
    if re.search(r"\b(?:nvme|sata)\s*\?", value):
        issues.append("uncertain_interface")
    new, used = bool(_NEW.search(value)), bool(_USED.search(value))
    condition = "new" if new and not used else "used" if used and not new else None
    if new and used:
        issues.append("conflicting_conditions")
    sold, sold_ambiguous = _sold(text)
    if sold_ambiguous:
        issues.append("ambiguous_availability")
    if _BUY.search(value):
        issues.append("buy_request")
    brand, model = _brand_model(text)
    return Offer(
        ordinal=ordinal, text=text, search_text=combined, drive_type=drive_type,
        brand=brand, model=model, capacity_gb=capacity,
        capacity_raw=capacities[0][1] if len(unique_caps) == 1 else None,
        interface=interface, form_factor="m2" if _M2.search(value) else None,
        condition=condition, price=price, currency=currency, price_kind=kind,
        availability="sold" if global_sold or sold else "ambiguous" if sold_ambiguous else "unmarked",
        issues=tuple(dict.fromkeys(issues)),
    )


def parse_post(text: str, *, has_media: bool = False) -> ParsedPost:
    if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH:
        raise ValueError("Текст публикации отсутствует или превышает допустимую длину.")
    if not text.strip():
        return ParsedPost("unsupported_media" if has_media else "no_text", issues=("no_text",))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    anchors: list[int] = []
    for index, line in enumerate(lines):
        value = normalize(line)
        caps = bool(_CAPACITY.search(value))
        price = bool(_PRICE.search(value))
        brand, model = _brand_model(line)
        if (caps and (brand or price)) or (brand and model):
            anchors.append(index)
    if len(anchors) > MAX_ITEMS:
        return ParsedPost("needs_review", issues=("too_many_items",))
    global_sold = any(_GLOBAL_SOLD.fullmatch(normalize(line)) for line in lines)
    if len(anchors) <= 1:
        offers = (_extract(text.strip(), "", 0, global_sold, False),)
    else:
        header = "\n".join(lines[:anchors[0]])
        blocks = ["\n".join(lines[start:end]) for start, end in zip(anchors, anchors[1:] + [len(lines)])]
        # Price on a shared footer cannot safely be assigned to one of several goods.
        missing_prices = any(extract_price(block)[2] == "unknown" for block in blocks)
        ambiguous = missing_prices or any(len({v for v, _ in extract_capacities(block)}) > 1 for block in blocks)
        offers = tuple(_extract(block, header, i, global_sold, ambiguous) for i, block in enumerate(blocks))
        # A free-standing cross or unattributed sold prose in a list is not reliable item attribution.
        loose_marker = any(
            (_CROSS.fullmatch(normalize(line).replace("\ufe0f", "")) or _SOLD.search(normalize(line)))
            and not _GLOBAL_SOLD.fullmatch(normalize(line))
            and not _CAPACITY.search(normalize(line)) and not _BRAND.search(normalize(line))
            for line in lines
        )
        if loose_marker and not global_sold:
            offers = tuple(replace(offer, issues=tuple(dict.fromkeys((*offer.issues, "ambiguous_availability")))) for offer in offers)
    status = "needs_review" if any(offer.issues for offer in offers) else "parsed"
    return ParsedPost(status, offers)
