"""Bounded local UI queries over the existing schema; no network or credentials."""

from __future__ import annotations

import json
from html.parser import HTMLParser
import secrets
import time

from .models import SearchRule, checked_key, timestamp, utc_now
from .parser import normalize
from .storage import Store
from .telegram_source import APPROVED_USERNAMES, TEST_USERNAME

PAGE_SIZE = 24
MAX_PRESETS = 200
DECISIONS = ("matched", "needs_review", "rejected", "unassessed")
JOB_STATES = ("held", "pending", "in_flight", "sent", "unknown", "failed", "cancelled")


def _offer_in_search(payload: str, config: str) -> int:
    """Scope rejected UI rows to the product, without changing stored decisions or sends."""
    offer, rule = json.loads(payload), json.loads(config)
    if offer.get("drive_type") not in ("ssd", "hdd"):
        return 0
    for key in ("drive_type", "interface"):
        if rule.get(key) and offer.get(key) and offer[key] != rule[key]:
            return 0
    for key, allowed_key in (("capacity_gb", "capacities_gb"), ("brand", "brands"), ("model", "models")):
        actual, allowed = offer.get(key), rule.get(allowed_key, [])
        if allowed and actual is not None:
            if key == "capacity_gb" and actual not in allowed:
                return 0
            if key != "capacity_gb" and normalize(actual) not in {normalize(value) for value in allowed}:
                return 0
    words = rule.get("keywords", [])
    if words:
        text = normalize(offer.get("search_text", ""))
        hits = [normalize(word) in text for word in words]
        if not (all(hits) if rule.get("keyword_mode") == "all" else any(hits)):
            return 0
    return 1

_CURRENT = """
WITH current_offers AS (
 SELECT o.id,o.payload_json,p.message_id,p.revision,p.published_at,p.first_detected_at,
 s.source_key,s.name AS source_name,s.kind,s.enabled AS source_enabled,v.parse_status,
 CASE WHEN EXISTS(SELECT 1 FROM evaluations e JOIN rules r ON r.id=e.rule_id
      WHERE e.offer_id=o.id AND r.enabled=1 AND r.version=e.rule_version AND e.decision='matched') THEN 'matched'
 WHEN EXISTS(SELECT 1 FROM evaluations e JOIN rules r ON r.id=e.rule_id
      WHERE e.offer_id=o.id AND r.enabled=1 AND r.version=e.rule_version AND e.decision='needs_review') THEN 'needs_review'
 WHEN EXISTS(SELECT 1 FROM evaluations e JOIN rules r ON r.id=e.rule_id
      WHERE e.offer_id=o.id AND r.enabled=1 AND r.version=e.rule_version) THEN 'rejected'
 ELSE 'unassessed' END AS decision
 FROM offers o JOIN post_versions v ON v.id=o.version_id
 JOIN posts p ON p.id=v.post_id AND p.revision=v.revision
 JOIN sources s ON s.id=p.source_id WHERE p.unavailable_at IS NULL
)
"""

_PRESET_CURRENT = """
WITH current_offers AS (
 SELECT o.id,o.payload_json,p.message_id,p.revision,p.published_at,p.first_detected_at,
 s.source_key,s.name AS source_name,s.kind,s.enabled AS source_enabled,v.parse_status,
 CASE WHEN r.enabled=1 THEN COALESCE(e.decision,'unassessed') ELSE 'unassessed' END AS decision
 FROM offers o JOIN post_versions v ON v.id=o.version_id
 JOIN posts p ON p.id=v.post_id AND p.revision=v.revision
 JOIN sources s ON s.id=p.source_id JOIN rules r ON r.rule_key=?
 LEFT JOIN evaluations e ON e.offer_id=o.id AND e.rule_id=r.id AND e.rule_version=r.version
 WHERE p.unavailable_at IS NULL
)
"""


class _PlainCard(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_card(text: str) -> str:
    parser = _PlainCard()
    parser.feed(text[:8000])
    parser.close()
    return "".join(parser.parts)[:4000]


def positive_id(value: object, *, allow_zero: bool = False) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value < 2**63:
        raise ValueError("Некорректный идентификатор.")
    return value


def original_link(source: str, message_id: int, *, approved=APPROVED_USERNAMES) -> str | None:
    """Only generate references to the already approved public Telegram channels."""
    username = source.removeprefix("telegram:")
    if source.startswith("telegram:") and username in approved:
        from .telegram_source import public_username
        username = public_username(username)
        positive_id(message_id)
        return f"https://t.me/{username}/{message_id}"
    return None


class UIStore(Store):
    def __init__(self, path):
        super().__init__(path)
        self.connection.create_function("radar_in_search", 2, _offer_in_search, deterministic=True)
        deadline = time.monotonic() + 4
        self.connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)

    def overview(self) -> dict:
        counts = {state: 0 for state in DECISIONS}
        counts.update({row[0]: row[1] for row in self.connection.execute(
            _CURRENT + "SELECT decision,COUNT(*) FROM current_offers GROUP BY decision")})
        sources = [dict(row) for row in self.connection.execute(
            "SELECT s.source_key,s.name,s.kind,s.enabled,s.last_message_id,s.last_successful_ingest_at,"
            "s.last_error_code,t.channel_id,t.last_successful_check_at,t.retry_not_before,t.check_state "
            "FROM sources s LEFT JOIN telegram_state t ON t.source_id=s.id ORDER BY s.id LIMIT 101")]
        rules = self.connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        notifier = self.notifier()
        return {"counts": counts, "sources": sources[:100], "sources_truncated": len(sources) > 100,
                "preset_count": rules, "database": self.path.name,
                "synthetic": any(s["kind"] == "test" or s["source_key"] == "telegram:" + TEST_USERNAME for s in sources),
                "notifier": ({k: notifier[k] for k in ("enabled", "last_error_code", "last_successful_send_at", "not_before")}
                             if notifier else None), "as_of": timestamp(utc_now())}

    def feed(self, *, decision: str = "matched", source: str = "", before: int = 0, preset: str = "") -> dict:
        if decision not in (*DECISIONS, "all"):
            raise ValueError("Неизвестный фильтр ленты.")
        if source:
            checked_key(source)
        if preset:
            checked_key(preset)
            if not self.connection.execute("SELECT 1 FROM rules WHERE rule_key=?", (preset,)).fetchone():
                raise ValueError("Пресет не найден; обновите страницу.")
        positive_id(before, allow_zero=True)
        current, parameters = (_PRESET_CURRENT, (preset,)) if preset else (_CURRENT, ())
        scope_sql, scope_parameters = "", ()
        if decision == "rejected":
            if preset:
                scope_sql = " AND radar_in_search(payload_json,(SELECT config_json FROM rules WHERE rule_key=?))=1 "
                scope_parameters = (preset,)
            else:
                scope_sql = (" AND EXISTS(SELECT 1 FROM evaluations scope_e JOIN rules scope_r ON scope_r.id=scope_e.rule_id "
                             "WHERE scope_e.offer_id=current_offers.id AND scope_r.enabled=1 "
                             "AND scope_e.rule_version=scope_r.version AND scope_e.decision='rejected' "
                             "AND radar_in_search(current_offers.payload_json,scope_r.config_json)=1) ")
        rows = self.connection.execute(
            current + "SELECT * FROM current_offers WHERE (?='all' OR decision=?) "
            "AND (?='' OR source_key=?) AND (?=0 OR id<?) " + scope_sql + " ORDER BY id DESC LIMIT ?",
            parameters + (decision, decision, source, source, before, before) + scope_parameters + (PAGE_SIZE + 1,)).fetchall()
        items = []
        for row in rows[:PAGE_SIZE]:
            item = dict(row)
            offer = json.loads(item.pop("payload_json"))
            item["excerpt"] = offer.pop("text")[:400]
            offer.pop("search_text", None)
            item["offer"] = offer
            item["original_url"] = original_link(item["source_key"], item["message_id"], approved=self.telegram_usernames())
            item["evaluations"] = [
                {"name": e["name"], "decision": e["decision"], "reasons": json.loads(e["reasons_json"])}
                for e in self.connection.execute(
                    "SELECT r.config_json->>'$.name' AS name,e.decision,e.reasons_json FROM evaluations e "
                    "JOIN rules r ON r.id=e.rule_id AND r.version=e.rule_version AND r.enabled=1 "
                    "WHERE e.offer_id=? AND (?='' OR r.rule_key=?) "
                    "AND (?!='rejected' OR radar_in_search(?,r.config_json)=1) ORDER BY r.id LIMIT 200",
                    (row["id"], preset, preset, decision, row["payload_json"]))]
            items.append(item)
        return {"items": items, "next_before": items[-1]["id"] if len(rows) > PAGE_SIZE else None,
                "rejected_scope_version": 1}

    def post(self, source: str, message_id: int) -> dict:
        checked_key(source)
        positive_id(message_id)
        rows = self.connection.execute(
            "SELECT v.revision,v.raw_text,v.published_at,v.edited_at,v.detected_at,v.parse_status,"
            "v.parse_issues_json,v.has_media,p.unavailable_at,s.name AS source_name FROM post_versions v "
            "JOIN posts p ON p.id=v.post_id JOIN sources s ON s.id=p.source_id "
            "WHERE s.source_key=? AND p.message_id=? ORDER BY v.revision DESC LIMIT 21",
            (source, message_id)).fetchall()
        if not rows:
            raise ValueError("Сохранённая публикация не найдена.")
        return {"versions": [dict(row) for row in rows[:20]], "older_versions": len(rows) > 20,
                "original_url": original_link(source, message_id, approved=self.telegram_usernames()), "message_id": message_id}

    def presets(self) -> dict:
        rows = self.connection.execute("SELECT version,config_json FROM rules ORDER BY id DESC LIMIT ?",
                                       (MAX_PRESETS + 1,)).fetchall()
        return {"items": [{"version": r[0], "config": json.loads(r[1])} for r in rows[:MAX_PRESETS]],
                "truncated": len(rows) > MAX_PRESETS}

    def save_preset(self, config: dict, expected_version: int) -> int:
        positive_id(expected_version, allow_zero=True)
        if not isinstance(config, dict):
            raise ValueError("Ожидаются параметры пресета.")
        config = dict(config)
        if not config.get("key"):
            config["key"] = "preset:" + secrets.token_hex(8)
        rule = SearchRule.from_dict(config)
        if expected_version == 0 and self.connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0] >= MAX_PRESETS:
            raise ValueError("В интерфейсе доступно до 200 пресетов.")
        return self.put_rule(rule, expected_version=expected_version)

    def recheck_slice(self, after: int) -> dict:
        positive_id(after, allow_zero=True)
        with self.transaction() as db:
            rows = db.execute(
                "SELECT p.id,p.revision,v.id AS version_id FROM posts p JOIN sources s ON s.id=p.source_id "
                "JOIN post_versions v ON v.post_id=p.id AND v.revision=p.revision "
                "WHERE s.enabled=1 AND p.unavailable_at IS NULL AND p.id>? ORDER BY p.id LIMIT 51", (after,)).fetchall()
            matched = review = 0
            for row in rows[:50]:
                yes, maybe = self._evaluate_version(row["id"], row["version_id"], row["revision"],
                                                     new_post=False, now=timestamp(utc_now()))
                matched += yes
                review += maybe
            return {"posts": min(50, len(rows)), "matched": matched, "needs_review": review,
                    "next_after": rows[49]["id"] if len(rows) > 50 else None}

    def notification_page(self, *, status: str = "all", before: int = 0) -> dict:
        if status not in (*JOB_STATES, "all"):
            raise ValueError("Неизвестный статус уведомления.")
        positive_id(before, allow_zero=True)
        rows = self.connection.execute(
            "SELECT COALESCE(j.delivery_group,j.id) AS id,MIN(j.status) AS status,"
            "COUNT(DISTINCT j.status) AS status_count,MIN(j.reason) AS reason,COUNT(*) AS preset_count,MAX(j.attempts) AS attempts,"
            "MAX(j.updated_at) AS updated_at,MAX(j.sent_at) AS sent_at,MAX(j.delivery_text) AS delivery_text,"
            "MAX(j.last_error_code) AS last_error_code,MAX(j.retry_not_before) AS retry_not_before,"
            "s.name AS source_name,s.source_key,p.message_id FROM notification_jobs j "
            "JOIN posts p ON p.id=j.post_id JOIN sources s ON s.id=p.source_id "
            "WHERE (?=0 OR COALESCE(j.delivery_group,j.id)<?) "
            "GROUP BY COALESCE(j.delivery_group,j.id) "
            "HAVING (?='all' OR SUM(j.status=?)>0) ORDER BY id DESC LIMIT ?",
            (before, before, status, status, PAGE_SIZE + 1)).fetchall()
        items = []
        for row in rows[:PAGE_SIZE]:
            item = dict(row)
            # Display sent markup as text. No stored HTML is passed to a browser HTML sink.
            item["delivery_text"] = plain_card(item["delivery_text"] or "")
            item["original_url"] = original_link(item["source_key"], item["message_id"], approved=self.telegram_usernames())
            if item.pop("status_count") > 1:
                item["status"] = "mixed"
            items.append(item)
        return {"items": items, "next_before": items[-1]["id"] if len(rows) > PAGE_SIZE else None}
