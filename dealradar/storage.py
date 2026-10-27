"""SQLite persistence and atomic offline processing. No Telegram credentials belong here."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .filters import evaluate
from .models import (
    IncomingPost, Offer, ProcessResult, SearchRule, checked_key, read_timestamp, timestamp, utc_now,
)
from .parser import PARSER_VERSION, parse_post
from .notification_store import NOTIFICATION_SCHEMA, NotificationStore
from .process_lock import monitor_lock

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'test'),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_message_id INTEGER NOT NULL DEFAULT 0,
    last_successful_ingest_at TEXT,
    last_error_code TEXT
);
CREATE TABLE rules (
    id INTEGER PRIMARY KEY,
    rule_key TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    config_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE posts (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    edited_at TEXT,
    original_url TEXT,
    first_detected_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL,
    revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    UNIQUE (source_id, message_id)
);
CREATE TABLE post_versions (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    original_url TEXT,
    published_at TEXT NOT NULL,
    edited_at TEXT,
    has_media INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    parse_issues_json TEXT NOT NULL,
    UNIQUE (post_id, revision)
);
CREATE TABLE offers (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES post_versions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (version_id, ordinal)
);
CREATE TABLE evaluations (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    rule_version INTEGER NOT NULL,
    rule_snapshot_json TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('matched', 'rejected', 'needs_review')),
    reasons_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    UNIQUE (offer_id, rule_id, rule_version)
);
CREATE TABLE notification_jobs (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    target_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('held', 'cancelled')),
    reason TEXT NOT NULL,
    offer_ordinals_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (post_id, rule_id)
);
CREATE INDEX ix_versions_post ON post_versions(post_id, revision);
CREATE INDEX ix_evaluations_offer ON evaluations(offer_id, rule_id, rule_version);
"""

_LIVE_SCHEMA = """
CREATE TABLE telegram_state (
    source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL UNIQUE CHECK(channel_id > 0),
    username TEXT NOT NULL,
    boundary_id INTEGER NOT NULL CHECK(boundary_id >= 0),
    initialized_at TEXT NOT NULL,
    last_successful_check_at TEXT,
    retry_not_before TEXT,
    edit_before_id INTEGER,
    check_state TEXT NOT NULL DEFAULT 'never'
);
CREATE TABLE telegram_auth_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    retry_not_before TEXT NOT NULL,
    last_error_code TEXT NOT NULL
);
ALTER TABLE posts ADD COLUMN unavailable_at TEXT;
"""

# Rebuild only the parent table, keeping its name/IDs and child references intact.
_MIGRATE_SOURCES = """
CREATE TABLE sources_v2 (
    id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('test','telegram')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    last_message_id INTEGER NOT NULL DEFAULT 0,
    last_successful_ingest_at TEXT, last_error_code TEXT
);
INSERT INTO sources_v2 SELECT * FROM sources;
DROP TABLE sources;
ALTER TABLE sources_v2 RENAME TO sources;
"""


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _offer(payload: str) -> Offer:
    from decimal import Decimal

    data = json.loads(payload)
    data["price"] = Decimal(data["price"]) if data["price"] is not None else None
    data["issues"] = tuple(data["issues"])
    return Offer(**data)


class RuleConflict(ValueError):
    """A local editor tried to replace a newer saved preset."""


class MaintenanceConflict(ValueError):
    """A destructive local operation would discard an active platform pause."""


class Store(NotificationStore):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA trusted_schema = OFF")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, SCHEMA_VERSION):
                raise ValueError("Версия базы не поддерживается; файл не изменён.")
            if version == 0:
                existing = self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if existing:
                    raise ValueError("Это не пустая база радара; файл не изменён.")
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + _SCHEMA.replace("CHECK (kind = 'test')", "CHECK (kind IN ('test','telegram'))")
                    + _LIVE_SCHEMA + NOTIFICATION_SCHEMA + f"\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )
            elif version == 1:
                expected = {'sources', 'rules', 'posts', 'post_versions', 'offers', 'evaluations', 'notification_jobs'}
                actual = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if actual != expected:
                    raise ValueError("Структура старой базы не распознана; миграция отменена.")
                if self.connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Связи старой базы нарушены; миграция отменена.")
                self.connection.execute("PRAGMA foreign_keys = OFF")
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + _MIGRATE_SOURCES + _LIVE_SCHEMA + NOTIFICATION_SCHEMA
                    + "\nPRAGMA user_version = 3;\nCOMMIT;"
                )
                self.connection.execute("PRAGMA foreign_keys = ON")
                if self.connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Проверка связей базы не прошла.")
            elif version == 2:
                expected = {'sources', 'rules', 'posts', 'post_versions', 'offers', 'evaluations',
                            'notification_jobs', 'telegram_state', 'telegram_auth_state'}
                actual = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if actual != expected or self.connection.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Структура или связи базы этапа 2 не распознаны; миграция отменена.')
                self.connection.executescript('BEGIN IMMEDIATE;\n' + NOTIFICATION_SCHEMA
                                              + '\nPRAGMA user_version = 3;\nCOMMIT;')
            self.connection.execute("PRAGMA journal_mode = WAL")
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            self.connection.close()
            raise

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _checkpoint_deleted_history(self) -> bool:
        try:
            result = self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            return result[0] == 0
        except sqlite3.Error:
            # Deletion has committed; report a busy journal without pretending rollback.
            return False

    def clear_history(self, confirmation: str) -> dict:
        if confirmation != 'УДАЛИТЬ':
            raise ValueError('Для очистки требуется точное подтверждение УДАЛИТЬ.')
        with monitor_lock(self.path):
            self.connection.execute('PRAGMA secure_delete=ON')
            with self.transaction() as db:
                counts = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                          for table in ('posts', 'post_versions', 'offers', 'evaluations', 'notification_jobs')}
                # Move only the lower history boundary; never reset the saved cursor.
                db.execute('UPDATE telegram_state SET boundary_id=MAX(boundary_id,'
                           '(SELECT last_message_id FROM sources WHERE id=telegram_state.source_id)),'
                           'edit_before_id=NULL')
                db.execute('DELETE FROM posts')  # Foreign keys cascade through all history and outbox rows.
                db.execute('UPDATE notifier_settings SET last_successful_send_at=NULL WHERE singleton=1')
            checkpoint = self._checkpoint_deleted_history()
        return {'cleared': True, 'deleted': counts, 'checkpoint_complete': checkpoint}

    def forget_notifier(self, confirmation: str) -> dict:
        if confirmation != 'ОТВЯЗАТЬ':
            raise ValueError('Для удаления привязки требуется точное подтверждение ОТВЯЗАТЬ.')
        with monitor_lock(self.path):
            self.connection.execute('PRAGMA secure_delete=ON')
            with self.transaction() as db:
                current = self.notifier()
                if current and current['not_before'] and read_timestamp(current['not_before']) > utc_now():
                    raise MaintenanceConflict('Ещё действует пауза отправки. Дождись её окончания перед удалением привязки.')
                db.execute('DELETE FROM notification_jobs')
                db.execute('DELETE FROM notifier_settings')
            checkpoint = self._checkpoint_deleted_history()
        return {'forgotten': True, 'checkpoint_complete': checkpoint}

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def add_test_source(self, key: str, name: str) -> None:
        checked_key(key)
        if not key.startswith("test:"):
            raise ValueError("Этап 1 поддерживает только источники с префиксом test:.")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Укажите название тестового источника до 100 символов.")
        with self.transaction() as db:
            db.execute(
                "INSERT INTO sources(source_key,name,kind) VALUES (?,?,'test') "
                "ON CONFLICT(source_key) DO UPDATE SET name = excluded.name",
                (key, name),
            )

    def set_source_enabled(self, key: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled должен быть логическим значением.")
        with self.transaction() as db:
            result = db.execute("UPDATE sources SET enabled = ? WHERE source_key = ?", (int(enabled), key))
            if result.rowcount != 1:
                raise ValueError("Источник не найден.")
            if not enabled:
                db.execute(
                    "UPDATE notification_jobs SET status=CASE WHEN status='in_flight' THEN status ELSE 'cancelled' END, "
                    "reason='source_disabled', updated_at=? "
                    "WHERE status IN ('held','pending','in_flight') AND post_id IN "
                    "(SELECT p.id FROM posts p JOIN sources s ON s.id=p.source_id WHERE s.source_key=?)",
                    (timestamp(utc_now()), key),
                )

    def put_rule(self, rule: SearchRule, *, expected_version: int | None = None) -> int:
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise ValueError("Некорректная версия пресета.")
        payload = encode(rule.to_dict())
        with self.transaction() as db:
            current = db.execute("SELECT * FROM rules WHERE rule_key = ?", (rule.key,)).fetchone()
            if expected_version is not None and expected_version != (current["version"] if current else 0):
                raise RuleConflict("Пресет уже изменён в другом окне. Обновите список и откройте его заново.")
            if current and current["config_json"] == payload:
                return current["version"]
            version = current["version"] + 1 if current else 1
            now = timestamp(utc_now())
            db.execute(
                "INSERT INTO rules(rule_key,version,enabled,config_json,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(rule_key) DO UPDATE SET version=excluded.version,enabled=excluded.enabled,"
                "config_json=excluded.config_json,updated_at=excluded.updated_at",
                (rule.key, version, int(rule.enabled), payload, now),
            )
            if current:
                db.execute(
                    "UPDATE notification_jobs SET status=CASE WHEN status='in_flight' THEN status ELSE 'cancelled' END,"
                    "reason='rule_changed',updated_at=? "
                    "WHERE rule_id=? AND status IN ('held','pending','in_flight')", (now, current["id"]),
                )
            return version

    def source_error(self, key: str, code: str) -> None:
        if code not in {"invalid_input", "storage_error", "access_denied", "rate_limited", "timeout",
                        "network_error", "auth_required", "identity_changed", "protocol_error"}:
            raise ValueError("Неизвестный код ошибки; произвольные тексты ошибок не сохраняются.")
        with self.transaction() as db:
            if db.execute("UPDATE sources SET last_error_code=? WHERE source_key=?", (code, key)).rowcount != 1:
                raise ValueError("Источник не найден.")

    def _evaluate_version(self, post_id: int, version_id: int, revision: int, *, new_post: bool, now: str) -> tuple[int, int]:
        db = self.connection
        matched = review = 0
        offers = db.execute("SELECT id,payload_json FROM offers WHERE version_id=? ORDER BY ordinal", (version_id,)).fetchall()
        for row in db.execute("SELECT * FROM rules WHERE enabled=1").fetchall():
            rule = SearchRule.from_dict(json.loads(row["config_json"]))
            ordinals: list[int] = []
            for saved in offers:
                offer = _offer(saved["payload_json"])
                result = evaluate(offer, rule)
                db.execute(
                    "INSERT INTO evaluations(offer_id,rule_id,rule_version,rule_snapshot_json,decision,"
                    "reasons_json,evidence_json,evaluated_at) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(offer_id,rule_id,rule_version) DO UPDATE SET decision=excluded.decision,"
                    "reasons_json=excluded.reasons_json,evidence_json=excluded.evidence_json,evaluated_at=excluded.evaluated_at",
                    (saved["id"], row["id"], row["version"], row["config_json"], result.status,
                     encode(result.reasons), encode(result.evidence), now),
                )
                matched += result.status == "matched"
                review += result.status == "needs_review"
                if result.status == "matched":
                    ordinals.append(offer.ordinal)
            if not ordinals:
                db.execute(
                    "UPDATE notification_jobs SET status=CASE WHEN status='in_flight' THEN status ELSE 'cancelled' END,"
                    "reason='no_current_match',updated_at=? "
                    "WHERE post_id=? AND rule_id=? AND status IN ('held','pending','in_flight')", (now, post_id, row["id"]),
                )
            elif new_post:
                config = self.notifier()
                source = db.execute('SELECT s.kind FROM sources s JOIN posts p ON p.source_id=s.id WHERE p.id=?', (post_id,)).fetchone()
                deliver = bool(config and config['enabled'] and source['kind'] == 'telegram')
                db.execute(
                    "INSERT INTO notification_jobs(post_id,rule_id,target_revision,status,reason,"
                    "offer_ordinals_json,created_at,updated_at,rule_version,bot_id,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(post_id,rule_id) DO NOTHING",
                    (post_id, row["id"], revision, 'pending' if deliver else 'held',
                     'new_matching_post' if deliver else 'preview_only', encode(ordinals), now, now, row['version'],
                     config['bot_id'] if deliver else None, config['owner_id'] if deliver else None),
                )
            else:
                # Never create/revive a delivery on an edit or on a rules-only recheck.
                db.execute(
                    "UPDATE notification_jobs SET target_revision=?,offer_ordinals_json=?,updated_at=? "
                    "WHERE post_id=? AND rule_id=? AND status IN ('held','pending')",
                    (revision, encode(ordinals), now, post_id, row["id"]),
                )
        return matched, review

    def process(self, source_key: str, post: IncomingPost, *, detected_at: datetime | None = None,
                origin: str = "test") -> ProcessResult:
        now = timestamp(detected_at or utc_now())
        fingerprint = hashlib.sha256(encode(post.to_dict()).encode("utf-8")).hexdigest()
        parsed = parse_post(post.text, has_media=post.has_media)
        with self.transaction() as db:
            source = db.execute("SELECT * FROM sources WHERE source_key=?", (source_key,)).fetchone()
            if not source or not source["enabled"]:
                raise ValueError("Источник не найден или отключён.")
            if origin not in ('test', 'telegram') or source['kind'] != origin:
                raise ValueError("Ручной импорт и реальный коннектор используют разные источники.")
            if origin == 'telegram':
                boundary = db.execute("SELECT boundary_id FROM telegram_state WHERE source_id=?", (source['id'],)).fetchone()
                if not boundary or post.message_id <= boundary[0]:
                    raise ValueError("Публикация предшествует согласованной начальной точке.")
            current = db.execute("SELECT * FROM posts WHERE source_id=? AND message_id=?", (source["id"], post.message_id)).fetchone()
            if current and current["published_at"] != timestamp(post.published_at):
                raise ValueError("Дата первоначальной публикации не должна меняться.")
            db.execute(
                "UPDATE sources SET last_message_id=MAX(last_message_id,?),last_successful_ingest_at=?,"
                "last_error_code=NULL WHERE id=?", (post.message_id, now, source["id"]),
            )
            if current and current["fingerprint"] == fingerprint:
                return ProcessResult("duplicate", current["id"], current["revision"])
            if current and post.version_time < read_timestamp(current["edited_at"] or current["published_at"]):
                return ProcessResult("stale_ignored", current["id"], current["revision"])
            if current and post.edited_at is None:
                raise ValueError("Для изменения существующей публикации нужна дата edited_at.")
            revision = current["revision"] + 1 if current else 1
            if current:
                post_id = current["id"]
                db.execute(
                    "UPDATE posts SET edited_at=?,original_url=?,last_detected_at=?,revision=?,fingerprint=? WHERE id=?",
                    (timestamp(post.edited_at) if post.edited_at else None, post.original_url, now, revision, fingerprint, post_id),
                )
            else:
                cursor = db.execute(
                    "INSERT INTO posts(source_id,message_id,published_at,edited_at,original_url,"
                    "first_detected_at,last_detected_at,revision,fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
                    (source["id"], post.message_id, timestamp(post.published_at),
                     timestamp(post.edited_at) if post.edited_at else None, post.original_url, now, now, revision, fingerprint),
                )
                post_id = cursor.lastrowid
            cursor = db.execute(
                "INSERT INTO post_versions(post_id,revision,raw_text,original_url,published_at,edited_at,"
                "has_media,detected_at,parser_version,parse_status,parse_issues_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (post_id, revision, post.text, post.original_url, timestamp(post.published_at),
                 timestamp(post.edited_at) if post.edited_at else None, int(post.has_media), now,
                 PARSER_VERSION, parsed.status, encode(parsed.issues)),
            )
            version_id = cursor.lastrowid
            db.executemany("INSERT INTO offers(version_id,ordinal,payload_json) VALUES (?,?,?)",
                           [(version_id, offer.ordinal, encode(offer.to_dict())) for offer in parsed.offers])
            matched, review = self._evaluate_version(post_id, version_id, revision, new_post=current is None, now=now)
            return ProcessResult("edited" if current else "created", post_id, revision, matched, review)

    def recheck(self) -> dict[str, int]:
        """Apply current rules without sending or creating notifications for history."""
        matched = review = 0
        with self.transaction() as db:
            rows = db.execute(
                "SELECT p.id,p.revision,v.id AS version_id FROM posts p "
                "JOIN sources s ON s.id=p.source_id JOIN post_versions v ON v.post_id=p.id AND v.revision=p.revision "
                "WHERE s.enabled=1"
            ).fetchall()
            for row in rows:
                yes, maybe = self._evaluate_version(row["id"], row["version_id"], row["revision"], new_post=False, now=timestamp(utc_now()))
                matched += yes
                review += maybe
        return {"posts": len(rows), "matched": matched, "needs_review": review}

    def sources(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT s.*,t.channel_id,t.username,t.boundary_id,t.initialized_at,"
            "t.last_successful_check_at,t.retry_not_before,t.edit_before_id,t.check_state "
            "FROM sources s LEFT JOIN telegram_state t ON t.source_id=s.id ORDER BY s.id")]

    def check_telegram_scope(self, username: str) -> None:
        from .telegram_source import APPROVED_USERNAMES, TEST_USERNAME, public_username
        username = public_username(username)
        rows = self.sources()
        if any(row['kind'] != 'telegram' for row in rows):
            raise ValueError('Telegram и офлайн-примеры должны быть в разных базах.')
        if rows and any((row['source_key'] == 'telegram:' + TEST_USERNAME) != (username == TEST_USERNAME) for row in rows):
            raise ValueError('Учебный testirovk4 должен оставаться в отдельной базе.')
        if username not in APPROVED_USERNAMES and not any(row['source_key'] == 'telegram:' + username for row in rows):
            raise ValueError('Сначала добавьте канал в Источниках; чтение несогласованного канала запрещено.')

    def add_telegram_source(self, address: str) -> str:
        from .telegram_source import MAX_SOURCES, TEST_USERNAME, public_username
        username = public_username(address)
        key = 'telegram:' + username
        with self.transaction() as db:
            rows = self.sources()
            if any(row['source_key'] == key for row in rows):
                raise ValueError('Этот источник уже добавлен.')
            if any(row['kind'] != 'telegram' for row in rows):
                raise ValueError('Используйте рабочую базу Telegram, а не базу офлайн-примеров.')
            if rows and any((row['source_key'] == 'telegram:' + TEST_USERNAME) != (username == TEST_USERNAME) for row in rows):
                raise ValueError('Учебный testirovk4 должен оставаться в отдельной базе.')
            if len(rows) >= MAX_SOURCES:
                raise ValueError('В одной базе допускается до пяти каналов.')
            name = 'СИНТЕТИЧЕСКИЙ Telegram-тест: ' + username if username == TEST_USERNAME else username
            db.execute("INSERT INTO sources(source_key,name,kind) VALUES (?,?,'telegram')", (key, name))
        return key

    def telegram_usernames(self, *, enabled_only=False) -> tuple[str, ...]:
        from .telegram_source import public_username
        return tuple(public_username(row['source_key'].removeprefix('telegram:')) for row in self.sources()
                     if row['kind'] == 'telegram' and (not enabled_only or row['enabled']))

    def bind_telegram(self, username: str, channel_id: int, boundary_id: int) -> str:
        from .telegram_source import MAX_SOURCES, TEST_USERNAME, public_username
        username = public_username(username)
        if type(channel_id) is not int or not 0 < channel_id < 2**63:
            raise ValueError("Не согласованный канал.")
        if type(boundary_id) is not int or not 0 <= boundary_id < 2**31:
            raise ValueError("Некорректная начальная точка.")
        key = 'telegram:' + username
        with self.transaction() as db:
            self.check_telegram_scope(username)
            current = db.execute("SELECT s.id,t.channel_id FROM sources s LEFT JOIN telegram_state t "
                                 "ON t.source_id=s.id WHERE s.source_key=?", (key,)).fetchone()
            if current and current['channel_id'] is not None:
                if current['channel_id'] != channel_id:
                    raise ValueError("Числовой ID канала изменился; автоматическая перепривязка запрещена.")
                return key
            other = db.execute('SELECT source_id FROM telegram_state WHERE channel_id=?', (channel_id,)).fetchone()
            if other:
                raise ValueError('Этот числовой ID уже закреплён за другим источником.')
            if current and not db.execute('SELECT enabled FROM sources WHERE id=?', (current['id'],)).fetchone()[0]:
                raise ValueError('Источник приостановлен.')
            now = timestamp(utc_now())
            name = 'СИНТЕТИЧЕСКИЙ Telegram-тест: ' + username if username == TEST_USERNAME else username
            if current:
                source_id = current['id']
                db.execute('UPDATE sources SET last_message_id=? WHERE id=?', (boundary_id, source_id))
            else:
                if len(self.sources()) >= MAX_SOURCES:
                    raise ValueError('В одной базе допускается до пяти каналов.')
                result = db.execute("INSERT INTO sources(source_key,name,kind,last_message_id) VALUES (?,?,'telegram',?)",
                                    (key, name, boundary_id))
                source_id = result.lastrowid
            db.execute("INSERT INTO telegram_state(source_id,channel_id,username,boundary_id,initialized_at) "
                       "VALUES (?,?,?,?,?)", (source_id, channel_id, username, boundary_id, now))
        return key

    def telegram_status(self, key: str) -> dict[str, Any] | None:
        return next((source for source in self.sources() if source['source_key'] == key and source['kind'] == 'telegram'), None)

    def checked_telegram(self, key: str, upper_id: int) -> None:
        with self.transaction() as db:
            source = db.execute("SELECT id,last_message_id FROM sources WHERE source_key=? AND kind='telegram' AND enabled=1", (key,)).fetchone()
            if not source or type(upper_id) is not int or not source['last_message_id'] <= upper_id < 2**31:
                raise ValueError("Некорректное положение проверки.")
            db.execute("UPDATE sources SET last_message_id=?,last_error_code=NULL WHERE id=?", (upper_id, source['id']))
            db.execute("UPDATE telegram_state SET last_successful_check_at=?,retry_not_before=NULL,check_state='ok' WHERE source_id=?",
                       (timestamp(utc_now()), source['id']))

    def telegram_retry(self, key: str, code: str, until: datetime | None) -> None:
        if code not in {'timeout', 'network_error', 'access_denied', 'rate_limited', 'auth_required',
                        'identity_changed', 'protocol_error', 'invalid_input'}:
            raise ValueError("Неизвестный код ошибки.")
        with self.transaction() as db:
            db.execute("UPDATE sources SET last_error_code=? WHERE source_key=? AND kind='telegram'", (code, key))
            db.execute("UPDATE telegram_state SET retry_not_before=?,check_state='error' WHERE source_id=(SELECT id FROM sources WHERE source_key=?)",
                       (timestamp(until) if until else None, key))

    def telegram_check_state(self, key: str, state: str) -> None:
        if state not in ('checking', 'catching_up'):
            raise ValueError("Некорректное состояние проверки.")
        with self.transaction() as db:
            db.execute("UPDATE telegram_state SET check_state=? WHERE source_id=(SELECT id FROM sources WHERE source_key=?)", (state, key))

    def auth_pause(self) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT retry_not_before,last_error_code FROM telegram_auth_state WHERE singleton=1").fetchone()
        return dict(row) if row else None

    def pause_auth(self, until: datetime, code: str) -> None:
        if code not in ('login_attempt', 'rate_limited'):
            raise ValueError('Некорректный код паузы входа.')
        with self.transaction() as db:
            db.execute("INSERT INTO telegram_auth_state(singleton,retry_not_before,last_error_code) VALUES (1,?,?) "
                       "ON CONFLICT(singleton) DO UPDATE SET retry_not_before=MAX(retry_not_before,excluded.retry_not_before),"
                       "last_error_code=excluded.last_error_code", (timestamp(until), code))

    def edit_ids(self, key: str, before: int | None, limit: int = 100) -> list[int]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Лимит проверки правок 1–100.")
        return [row[0] for row in self.connection.execute(
            "SELECT p.message_id FROM posts p JOIN sources s ON s.id=p.source_id "
            "WHERE s.source_key=? AND p.unavailable_at IS NULL AND (? IS NULL OR p.message_id<?) "
            "ORDER BY p.message_id DESC LIMIT ?", (key, before, before, limit))]

    def advance_edit_scan(self, key: str, before: int | None) -> None:
        with self.transaction() as db:
            db.execute("UPDATE telegram_state SET edit_before_id=? WHERE source_id=(SELECT id FROM sources WHERE source_key=?)", (before, key))

    def unavailable(self, key: str, message_id: int) -> None:
        with self.transaction() as db:
            post = db.execute("SELECT p.id FROM posts p JOIN sources s ON s.id=p.source_id "
                              "WHERE s.source_key=? AND p.message_id=?", (key, message_id)).fetchone()
            if post:
                now = timestamp(utc_now())
                db.execute("UPDATE posts SET unavailable_at=? WHERE id=?", (now, post['id']))
                db.execute("UPDATE notification_jobs SET status=CASE WHEN status='in_flight' THEN status ELSE 'cancelled' END,"
                           "reason='unavailable',updated_at=? "
                           "WHERE post_id=? AND status IN ('held','pending','in_flight')", (now, post['id']))

    def rules(self) -> list[dict[str, Any]]:
        return [{"version": row["version"], "config": json.loads(row["config_json"])}
                for row in self.connection.execute("SELECT * FROM rules ORDER BY id")]

    def jobs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT j.*,s.source_key,p.message_id,r.rule_key FROM notification_jobs j "
            "JOIN posts p ON p.id=j.post_id JOIN sources s ON s.id=p.source_id JOIN rules r ON r.id=j.rule_id ORDER BY j.id"
        )
        return [{**dict(row), "offer_ordinals": json.loads(row["offer_ordinals_json"])} for row in rows]

    def offers(self, *, decision: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if decision not in (None, "matched", "rejected", "needs_review") or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Некорректный фильтр или лимит (1–500).")
        rows = self.connection.execute(
            "SELECT o.id,o.payload_json,p.message_id,p.revision,p.original_url,p.first_detected_at,"
            "s.source_key,s.name,v.parse_status,v.id AS version_id FROM offers o "
            "JOIN post_versions v ON v.id=o.version_id JOIN posts p ON p.id=v.post_id AND p.revision=v.revision "
            "JOIN sources s ON s.id=p.source_id "
            "WHERE p.unavailable_at IS NULL AND (? IS NULL OR EXISTS (SELECT 1 FROM evaluations e JOIN rules r ON r.id=e.rule_id "
            "WHERE e.offer_id=o.id AND e.rule_version=r.version AND r.enabled=1 AND e.decision=?)) "
            "ORDER BY p.id DESC,o.ordinal LIMIT ?", (decision, decision, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            judgments = self.connection.execute(
                "SELECT r.rule_key,e.decision,e.reasons_json,e.evidence_json FROM evaluations e "
                "JOIN rules r ON r.id=e.rule_id AND r.version=e.rule_version AND r.enabled=1 WHERE e.offer_id=?", (row["id"],),
            ).fetchall()
            result.append({
                "source": row["source_key"], "source_name": row["name"], "message_id": row["message_id"],
                "revision": row["revision"], "original_url": row["original_url"], "first_detected_at": row["first_detected_at"],
                "parse_status": row["parse_status"], "offer": json.loads(row["payload_json"]),
                "evaluations": [{"rule": item["rule_key"], "decision": item["decision"],
                                 "reasons": json.loads(item["reasons_json"]), "evidence": json.loads(item["evidence_json"])} for item in judgments],
            })
        return result

    def history(self, source_key: str, message_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT v.* FROM post_versions v JOIN posts p ON p.id=v.post_id "
            "JOIN sources s ON s.id=p.source_id WHERE s.source_key=? AND p.message_id=? ORDER BY v.revision",
            (source_key, message_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        counts = {table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("sources", "rules", "posts", "post_versions", "offers", "notification_jobs")}
        return {"stage": 3, "network_enabled": False, "telegram_connected": False,
                "connection_note": "Команда status читает базу; активную сессию она не проверяет.", "counts": counts}
