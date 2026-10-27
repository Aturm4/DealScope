"""Durable notification outbox, containing IDs and delivery history, never tokens."""

from __future__ import annotations

from datetime import timedelta
import json

from .bot_api import checked_user_id
from .filters import evaluate
from .models import SearchRule, timestamp, utc_now

NOTIFICATION_SCHEMA = """
CREATE TABLE notifier_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    bot_id INTEGER NOT NULL CHECK(bot_id>0),
    owner_id INTEGER NOT NULL CHECK(owner_id>0),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    enabled_at TEXT NOT NULL,
    not_before TEXT,
    last_error_code TEXT,
    last_successful_send_at TEXT
);
CREATE TABLE notification_jobs_v3 (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    target_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('held','pending','in_flight','sent','unknown','failed','cancelled')),
    reason TEXT NOT NULL,
    offer_ordinals_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    rule_version INTEGER,
    bot_id INTEGER,
    owner_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_not_before TEXT,
    last_error_code TEXT,
    delivery_group INTEGER,
    delivery_text TEXT,
    telegram_message_id INTEGER,
    sent_at TEXT,
    UNIQUE(post_id,rule_id)
);
INSERT INTO notification_jobs_v3(id,post_id,rule_id,target_revision,status,reason,
                               offer_ordinals_json,created_at,updated_at)
SELECT id,post_id,rule_id,target_revision,status,reason,offer_ordinals_json,created_at,updated_at
FROM notification_jobs;
DROP TABLE notification_jobs;
ALTER TABLE notification_jobs_v3 RENAME TO notification_jobs;
CREATE INDEX ix_notification_due ON notification_jobs(status,retry_not_before,post_id);
"""


class NotificationStore:
    def notifier(self) -> dict | None:
        row = self.connection.execute('SELECT * FROM notifier_settings WHERE singleton=1').fetchone()
        return dict(row) if row else None

    def link_notifier(self, bot_id: int, owner_id: int, *, now=utc_now) -> None:
        checked_user_id(bot_id)
        checked_user_id(owner_id)
        with self.transaction() as db:
            current = self.notifier()
            if current and (current['bot_id'], current['owner_id']) != (bot_id, owner_id):
                raise ValueError('Бот и получатель уже закреплены за этой базой; автоматическая замена запрещена.')
            if current and current['enabled']:
                return
            point = timestamp(now())
            # Old preview/pending history is never flushed when delivery is enabled.
            db.execute("UPDATE notification_jobs SET status=CASE WHEN status='in_flight' THEN status ELSE 'cancelled' END,"
                       "reason='before_enable',updated_at=? "
                       "WHERE status IN ('held','pending','in_flight')", (point,))
            db.execute('INSERT INTO notifier_settings(singleton,bot_id,owner_id,enabled,enabled_at) '
                       'VALUES (1,?,?,1,?) ON CONFLICT(singleton) DO UPDATE SET enabled=1,enabled_at=excluded.enabled_at',
                       (bot_id, owner_id, point))

    def disable_notifier(self) -> None:
        with self.transaction() as db:
            db.execute('UPDATE notifier_settings SET enabled=0 WHERE singleton=1')
            db.execute("UPDATE notification_jobs SET status=CASE WHEN status='in_flight' THEN status ELSE 'cancelled' END,"
                       "reason='notifications_disabled',updated_at=? "
                       "WHERE status IN ('held','pending','in_flight')", (timestamp(utc_now()),))

    def recover_notifications(self, *, now=utc_now) -> int:
        # Caller holds the per-database monitor lock. A crash may occur after sendMessage.
        with self.transaction() as db:
            return db.execute("UPDATE notification_jobs SET status='unknown',reason='interrupted_send',"
                              "last_error_code='delivery_unconfirmed',updated_at=? WHERE status='in_flight'",
                              (timestamp(now()),)).rowcount

    def notification_candidates(self, *, now=utc_now, limit=5, source_key=None) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError('Не более пяти публикаций на цикл отправки.')
        point = timestamp(now())
        return [dict(row) for row in self.connection.execute(
            "SELECT DISTINCT s.source_key,p.message_id,p.id FROM notification_jobs j "
            "JOIN posts p ON p.id=j.post_id JOIN sources s ON s.id=p.source_id "
            "JOIN notifier_settings n ON n.singleton=1 AND n.enabled=1 AND n.bot_id=j.bot_id AND n.owner_id=j.owner_id "
            "WHERE j.status='pending' AND s.enabled=1 AND s.kind='telegram' "
            "AND (j.retry_not_before IS NULL OR j.retry_not_before<=?) "
            "AND (n.not_before IS NULL OR n.not_before<=?) AND (? IS NULL OR s.source_key=?) "
            "ORDER BY p.id LIMIT ?", (point, point, source_key, source_key, limit))]

    def claim_notification(self, source_key: str, message_id: int, bot_id: int, owner_id: int, *, now=utc_now):
        from .notifications import format_notification
        from .storage import _offer

        checked_user_id(bot_id)
        checked_user_id(owner_id)
        self.check_telegram_scope(source_key.removeprefix('telegram:'))
        point = timestamp(now())
        with self.transaction() as db:
            config = self.notifier()
            if (not config or not config['enabled'] or (config['bot_id'], config['owner_id']) != (bot_id, owner_id)
                    or (config['not_before'] and config['not_before'] > point)):
                return None
            post = db.execute('SELECT p.*,s.source_key,s.name,s.enabled,s.kind FROM posts p JOIN sources s '
                              'ON s.id=p.source_id WHERE s.source_key=? AND p.message_id=?', (source_key, message_id)).fetchone()
            if not post or not post['enabled'] or post['kind'] != 'telegram' or post['unavailable_at']:
                return None
            jobs = db.execute("SELECT j.*,r.version AS current_rule_version,r.config_json,r.enabled AS rule_enabled "
                              "FROM notification_jobs j JOIN rules r ON r.id=j.rule_id WHERE j.post_id=? "
                              "AND j.status='pending' AND j.bot_id=? AND j.owner_id=? "
                              "AND (j.retry_not_before IS NULL OR j.retry_not_before<=?) ORDER BY j.id",
                              (post['id'], bot_id, owner_id, point)).fetchall()
            offers = [_offer(row[0]) for row in db.execute('SELECT o.payload_json FROM offers o JOIN post_versions v '
                      'ON v.id=o.version_id WHERE v.post_id=? AND v.revision=? ORDER BY o.ordinal',
                      (post['id'], post['revision']))]
            active, selected, rules = [], {}, []
            for job in jobs:
                rule = SearchRule.from_dict(json.loads(job['config_json']))
                matching = [offer for offer in offers if evaluate(offer, rule).status == 'matched']
                if (not job['rule_enabled'] or job['rule_version'] != job['current_rule_version']
                        or not matching or job['attempts'] >= 3):
                    db.execute("UPDATE notification_jobs SET status='cancelled',reason='not_current',updated_at=? WHERE id=?",
                               (point, job['id']))
                    continue
                active.append(job)
                selected.update({offer.ordinal: offer for offer in matching})
                rules.append(rule)
            if not active:
                return None
            group = active[0]['id']
            text = format_notification(dict(post), list(selected.values()), rules)
            # Persist intent BEFORE the HTTP call. A restart cannot silently resend it.
            for job in active:
                db.execute("UPDATE notification_jobs SET status='in_flight',reason='sending',target_revision=?,"
                           "attempts=attempts+1,delivery_group=?,delivery_text=?,updated_at=? WHERE id=?",
                           (post['revision'], group, text, point, job['id']))
            db.execute('UPDATE notifier_settings SET not_before=? WHERE singleton=1',
                       (timestamp(now() + timedelta(seconds=2)),))
            return {'group_id': group, 'text': text}

    def finish_notification(self, group: int, *, message_id: int | None = None, failure=None, now=utc_now) -> None:
        from .bot_api import BotFailure
        if type(group) is not int or group <= 0:
            raise ValueError('Некорректная группа отправки.')
        if failure is not None and not isinstance(failure, BotFailure):
            raise ValueError('Ожидается безопасный код ошибки.')
        if failure is None and (type(message_id) is not int or not 0 < message_id < 2**31):
            raise ValueError('Отправка подтверждается ID сообщения Telegram.')
        point = timestamp(now())
        with self.transaction() as db:
            rows = db.execute("SELECT id,attempts,reason FROM notification_jobs WHERE delivery_group=? AND status='in_flight'",
                              (group,)).fetchall()
            if not rows:
                raise ValueError('Активная отправка не найдена.')
            until = timestamp(now() + timedelta(seconds=failure.retry_after)) if failure and failure.retry_after else None
            for row in rows:
                if failure is None:
                    state, reason = 'sent', 'telegram_confirmed'
                elif failure.ambiguous:
                    state, reason = 'unknown', 'delivery_unconfirmed'
                elif row['reason'] in {'source_disabled', 'notifications_disabled', 'rule_changed',
                                       'no_current_match', 'unavailable', 'before_enable'}:
                    # A pause/change while HTTP was in flight must survive re-enabling.
                    # Confirmed success and ambiguous delivery above remain truthful.
                    state, reason = 'cancelled', row['reason']
                elif failure.code == 'rate_limited' and until and row['attempts'] < 3:
                    state, reason = 'pending', 'rate_limited'
                else:
                    state, reason = 'failed', 'send_rejected'
                db.execute('UPDATE notification_jobs SET status=?,reason=?,retry_not_before=?,last_error_code=?, '
                           'telegram_message_id=?,sent_at=?,updated_at=? WHERE id=?',
                           (state, reason, until, failure.code if failure else None, message_id,
                            point if failure is None else None, point, row['id']))
            if failure:
                db.execute('UPDATE notifier_settings SET last_error_code=?,not_before=CASE WHEN ? IS NULL THEN not_before '
                           'WHEN not_before IS NULL THEN ? ELSE MAX(not_before,?) END WHERE singleton=1',
                           (failure.code, until, until, until))
                if failure.code in ('token_rejected', 'recipient_unavailable'):
                    db.execute('UPDATE notifier_settings SET enabled=0 WHERE singleton=1')
            else:
                db.execute('UPDATE notifier_settings SET last_error_code=NULL,last_successful_send_at=? WHERE singleton=1', (point,))

    def resolve_notification(self, job_id: int, outcome: str, *, expected_source=None, expected_message_id=None) -> None:
        if type(job_id) is not int or job_id <= 0 or outcome not in ('sent', 'skip'):
            raise ValueError('Укажите ID и результат sent или skip после ручной проверки своего Telegram.')
        with self.transaction() as db:
            job = db.execute("SELECT j.delivery_group,s.source_key,p.message_id FROM notification_jobs j "
                             "JOIN posts p ON p.id=j.post_id JOIN sources s ON s.id=p.source_id "
                             "WHERE j.id=? AND j.status='unknown'", (job_id,)).fetchone()
            if not job:
                raise ValueError('Не найдена отправка с неизвестным результатом.')
            if expected_source is not None or expected_message_id is not None:
                if (type(expected_source) is not str or type(expected_message_id) is not int
                        or job['source_key'] != expected_source or job['message_id'] != expected_message_id):
                    raise ValueError('Отправка изменилась. Обнови историю уведомлений перед ручной отметкой.')
            db.execute("UPDATE notification_jobs SET status=?,reason=?,updated_at=? WHERE delivery_group=? AND status='unknown'",
                       ('sent' if outcome == 'sent' else 'cancelled', 'owner_confirmed' if outcome == 'sent' else 'owner_skipped',
                        timestamp(utc_now()), job['delivery_group']))
