# Запуск DealScope

## 1. Офлайн-интерфейс

Требуются Windows и Python 3.12. Команды выполняются из корня репозитория.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -B -m dealradar --db data/demo.sqlite3 demo
.\.venv\Scripts\python.exe -B -m dealradar --db data/demo.sqlite3 ui --port 8765
```

Адрес интерфейса — http://127.0.0.1:8765. Эта база содержит вымышленные объявления.
Демо и ручной импорт предназначены для отдельной тестовой базы.

Другие офлайн-команды:

```powershell
.\.venv\Scripts\python.exe -B -m dealradar --db data/demo.sqlite3 list
.\.venv\Scripts\python.exe -B -m dealradar --db data/demo.sqlite3 rules
.\.venv\Scripts\python.exe -B -m dealradar --db data/demo.sqlite3 recheck
.\.venv\Scripts\python.exe -B -m dealradar --db data/demo.sqlite3 check
```

## 2. Telegram-зависимости

Для Telegram-подключения и полного набора тестов установите зафиксированные
пакеты с проверкой SHA-256:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-build.txt
.\.venv\Scripts\python.exe -m pip install --require-hashes --no-build-isolation -r requirements-telegram.txt
.\.venv\Scripts\python.exe -B -m dealradar telegram-preflight
```

`requirements-build.txt` устанавливает инструмент сборки для закреплённого
pyaes. Дополнительные библиотеки не нужны для офлайн-демо.
Проверка `telegram-preflight` выполняется без входа и сетевого чтения Telegram.
Версии транспорта дополнительно проверяются приложением; обновлять их нужно
вместе с проверками совместимости.

## 3. Ручное подключение Telegram

Получите собственные API ID/hash в [Telegram API development tools](https://my.telegram.org).
Вход выполняется владельцем в своей консоли: API ID/hash, номер, код и 2FA-пароль
вводятся скрыто. Сессия пользовательского аккаунта хранится в памяти процесса.

Для мониторинга используется отдельная база, например `data/live.sqlite3`.
Публичные каналы добавляются и включаются владельцем; приложение не вступает
в них автоматически. Ценовые условия также задаёт владелец.

`telegram-run --help` показывает параметры чтения: `--channel` или
`--all-sources`, `--start-now`, `--watch`, `--interval`, `--notify`.
Первый запуск с `--start-now` фиксирует конец истории; он не загружает все
старые объявления.

Уведомления идут через отдельного бота, созданного владельцем в
[@BotFather](https://t.me/BotFather). Команда `bot-link` запрашивает токен в
консоли, выдаёт временную команду привязки и требует явного подтверждения ID.
Токен не передаётся аргументом командной строки и не хранится в конфигурации Git.

Для остановки мониторинга нажмите Ctrl+C один раз и дождитесь
`logout_confirmed: true`. При неподтверждённом выходе отзовите временную
сессию **Personal Deal Radar** в Telegram → Настройки → Устройства.

## 4. Необязательный приватный Sites-интерфейс

Основной локальный интерфейс работает без этого раздела.
Для приватного интерфейса нужен собственный сайт с доступом только владельцу;
адрес чужого рабочего сайта в репозитории не распространяется.

1. Скопируйте `site/site.example.json` в `site/site.json`.
2. Замените пример URL точным HTTPS-адресом своего приватного сайта.
   Текущий адаптер поддерживает адреса вида
   `https://your-site.your-account.chatgpt.site` без пути и параметров.
3. Для собственного развёртывания создайте `site/.openai/hosting.json`
   на основе `site/hosting.example.json` с идентификатором **своего** проекта.
4. Соберите шесть статических файлов:

```powershell
node site/build.mjs
```

Публикуется статический каталог `site/dist`. Локальная база, Telegram-сессия
и код подключения в него не входят.

После настройки `start-radar.cmd` запускает ручной desktop-сценарий.
Он открывает локальную страницу подключения; временный код связывает сайт
с процессом на ПК. Авторизацию Telegram выполняет сам владелец в своей консоли.
Закрытие локальной программы прекращает подключение.

`site/site.json` и `site/.openai/hosting.json` исключены из Git.
После ввода собственных настроек проверьте `git status` перед отправкой изменений.

## Технические имена

Название интерфейса и репозитория — **DealScope**. Имя Python-модуля
`dealradar`, команда `start-radar.cmd` и название временной Telegram-сессии
сохранены для совместимости.
