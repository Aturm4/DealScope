"""Small, fixed-host Bot API client. No user-account session or third-party SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
import getpass
import http.client
import json
import re
import ssl
import sys
import warnings

MAX_RESPONSE = 2 * 1024 * 1024


class BotFailure(Exception):
    """Public errors contain fixed codes only, never URLs, tokens or server text."""

    def __init__(self, code: str, *, ambiguous: bool = False, retry_after: int = 0):
        allowed = {'token_format', 'interactive_console_required', 'token_rejected',
                   'recipient_unavailable', 'rate_limited', 'network_error', 'protocol_error',
                   'bad_request', 'bot_in_use', 'webhook_present', 'identity_changed',
                   'owner_required', 'pairing_expired', 'pairing_not_confirmed'}
        self.code = code if code in allowed else 'protocol_error'
        self.ambiguous = bool(ambiguous)
        self.retry_after = retry_after if type(retry_after) is int and 0 < retry_after <= 31536000 else 0
        super().__init__(self.code)


def checked_user_id(value: int) -> int:
    if type(value) is not int or not 0 < value < 2**52:
        raise BotFailure('owner_required')
    return value


@dataclass(frozen=True, repr=False)
class BotToken:
    value: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.value, str) or not re.fullmatch(r'[1-9][0-9]{4,15}:[A-Za-z0-9_-]{30,128}', self.value):
            raise BotFailure('token_format')
        if int(self.value.split(':', 1)[0]) >= 2**52:
            raise BotFailure('token_format')

    @property
    def bot_id(self) -> int:
        return int(self.value.split(':', 1)[0])


def require_console() -> None:
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        raise BotFailure('interactive_console_required')


def prompt_token() -> BotToken:
    require_console()
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            value = getpass.getpass('Токен вашего отдельного бота (скрытый ввод): ')
        except (getpass.GetPassWarning, EOFError):
            raise BotFailure('interactive_console_required') from None
    return BotToken(value.strip())


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


class BotAPI:
    """Direct TLS only; no proxy environment, redirects, arbitrary methods or URLs.

    A recipient is fixed once. After that this client cannot receive updates.
    Every HTTP request is single-shot; send timeouts are potentially delivered.
    """

    def __init__(self, token: BotToken, *, owner_id: int | None = None):
        self._token = token
        self.bot_id = token.bot_id
        self._owner_id = checked_user_id(owner_id) if owner_id is not None else None

    def forget(self):
        # Drop references, without claiming to erase Python/OS memory.
        self._token = None

    @property
    def owner_id(self) -> int | None:
        return self._owner_id

    def _request(self, method: str, payload: dict):
        if self._token is None or method not in {'getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage'}:
            raise BotFailure('bad_request')
        sending = method == 'sendMessage'
        if sending and (self._owner_id is None or payload.get('chat_id') != self._owner_id):
            raise BotFailure('owner_required')
        if method == 'getUpdates' and self._owner_id is not None:
            raise BotFailure('bad_request')
        connection = None
        try:
            # http.client does not consult HTTP(S)_PROXY or follow Location headers.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_default_certs()
            # SSLContext construction deliberately does not honor SSLKEYLOGFILE.
            connection = http.client.HTTPSConnection('api.telegram.org', 443, timeout=35, context=context)
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            connection.request('POST', '/bot' + self._token.value + '/' + method, body=body,
                               headers={'Content-Type': 'application/json', 'Connection': 'close'})
            response = connection.getresponse()
            if response.status < 200 or response.status >= 600 or 300 <= response.status < 400:
                raise BotFailure('protocol_error', ambiguous=sending)
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise BotFailure('protocol_error', ambiguous=sending)
            result = json.loads(raw, object_pairs_hook=_unique_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise BotFailure('protocol_error', ambiguous=sending)
            if result['ok'] is False:
                code = result.get('error_code')
                parameters = result.get('parameters')
                retry = parameters.get('retry_after') if isinstance(parameters, dict) else None
                if code == 429 and type(retry) is int and 0 < retry <= 31536000:
                    raise BotFailure('rate_limited', retry_after=retry)
                errors = {400: 'bad_request', 401: 'token_rejected',
                          403: 'recipient_unavailable', 409: 'bot_in_use'}
                raise BotFailure(errors.get(code, 'protocol_error'),
                                 ambiguous=sending and code not in errors)
            if response.status != 200 or 'result' not in result:
                raise BotFailure('protocol_error', ambiguous=sending)
            return result['result']
        except BotFailure:
            raise
        except (OSError, http.client.HTTPException):
            raise BotFailure('network_error', ambiguous=sending) from None
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise BotFailure('protocol_error', ambiguous=sending) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    pass

    def identity(self) -> dict:
        result = self._request('getMe', {})
        if (not isinstance(result, dict) or result.get('is_bot') is not True
                or type(result.get('id')) is not int or result['id'] != self.bot_id
                or not isinstance(result.get('username'), str)
                or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,63}', result['username'])):
            raise BotFailure('identity_changed')
        return {'bot_id': self.bot_id, 'username': result['username']}

    def ensure_no_webhook(self):
        result = self._request('getWebhookInfo', {})
        if not isinstance(result, dict) or not isinstance(result.get('url'), str):
            raise BotFailure('protocol_error')
        if result['url']:
            # Never delete another integration's webhook automatically.
            raise BotFailure('webhook_present')

    def updates(self, offset: int = 0) -> list[dict]:
        if type(offset) is not int or not 0 <= offset < 2**52:
            raise BotFailure('bad_request')
        result = self._request('getUpdates', {'offset': offset, 'limit': 100, 'timeout': 20,
                                              'allowed_updates': ['message']})
        if not isinstance(result, list) or len(result) > 100:
            raise BotFailure('protocol_error')
        for update in result:
            if (not isinstance(update, dict) or type(update.get('update_id')) is not int
                    or not offset <= update['update_id'] < 2**52 - 1):
                raise BotFailure('protocol_error')
        return result

    def send(self, text: str) -> int:
        if self._owner_id is None:
            raise BotFailure('owner_required')
        if not isinstance(text, str) or not 1 <= len(text.encode('utf-16-le')) // 2 <= 4096:
            raise BotFailure('bad_request')
        result = self._request('sendMessage', {
            'chat_id': self._owner_id, 'text': text, 'parse_mode': 'HTML',
            'link_preview_options': {'is_disabled': True}, 'allow_paid_broadcast': False,
        })
        chat = result.get('chat') if isinstance(result, dict) else None
        if (not isinstance(chat, dict) or chat.get('type') != 'private'
                or type(chat.get('id')) is not int or chat['id'] != self._owner_id
                or type(result.get('message_id')) is not int or not 0 < result['message_id'] < 2**31):
            raise BotFailure('protocol_error', ambiguous=True)
        return result['message_id']
