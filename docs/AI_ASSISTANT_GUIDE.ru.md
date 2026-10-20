# AI ASSISTANT GUIDE — SHMAX (RU, канонический)

Руководство для ИИ-ассистентов, помогающих с установкой, эксплуатацией
и диагностикой SHMAX. Сначала прочитайте документ целиком; проверяйте
текущую версию и не запрашивайте у пользователя секреты.

## Идентичность продукта

SHMAX — самостоятельный шлюз **MAX ↔ Telegram** для собственного
сервера. MAX-аккаунт обычного пользователя подключается к SHMAX;
сообщения выбранных чатов MAX появляются в приватной
Telegram-супергруппе с форум-темами (один чат MAX = одна тема);
ответы ходят в обе стороны; автор подписан `[Имя]:`; медиа
доставляется с подписью.

## Поддерживаемая платформа

Debian 13; также ODROID-XU4 / ARMv7 (armhf), ~2 ГиБ RAM. Docker +
Compose v2. systemd.

## Архитектура (кратко)

- Веб (React/Vite) + FastAPI + SQLite/SQLAlchemy/Alembic в одном
  контейнере.
- Адаптеры: PyMax (MAX), aiogram (Bot API); транспорт Telegram:
  Direct/SOCKS5/HTTP/Xray (VLESS/Trojan, Reality; Vision только
  TCP). Xray запускается SHMAX-ом, SOCKS только 127.0.0.1.
- Очередь доставки + message_map + sent_native (эхо-реестр);
  авторство; ACL fail-closed; watchdog'и.
- На хосте: shmax-hostd (unix-сокет, фиксированный набор операций,
  БЕЗ произвольных команд), shmax-boot.service, инсталлятор.

## Файловая система

- `/opt/shmax` — деплой (compose.yaml, .env — настройки
  оператора). Заменяется обновлением (compose.yaml/.env
  сохраняются).
- `/var/lib/shmax` — ПОСТОЯННЫЕ данные: db/, secrets/ (AES-256-GCM),
  sessions/{max,telegram}/, state/, backups/ (0600), cache/.
- `/run/shmax/hostd.sock` — hostd (0660, группа shmax).

## Сервисы и порты

- Веб: настроенный bind (из `.env`), порт
  8787. Compose по умолчанию loopback, но инсталлятор может сохранить LAN-адрес
  в `.env`; диагностируйте фактический bind/порт. Публикация — через Docker compose.
- Boot: `shmax-boot.service` — ждёт появления bind-адреса,
  пересоздаёт контейнер при сломанной публикации порта, проверяет
  `docker port` и host-`/health/ready`.
- Health: `/health/live`, `/health/ready` — ВНУТРЕННИЕ проверки
  контейнера. Отсутствие публикации порта они НЕ показывают.

## Мастер настройки (ровно 9 шагов)

Admin → MAX (SMS/OTP, 2FA) → Telegram сеть → Telegram бот →
Telegram группа (`/shmax_connect <КОД>`) → MAX чаты →
Пользователи → Проверка → Готово.

## Важные факты

- MAX 2FA: если MAX пишет «Требуется установить 2FA», пользователь
  обязан включить 2FA в официальном приложении MAX.
- Первый getMe бота идёт через сохранённую сеть (без polling).
  Верификация токена НЕ включает слушатель; слушатель стартует на
  этапе пары и pair-code требует живой poller (ровно один).
- Обновления: только GitHub Releases; каналы stable/beta; SemVer;
  проверка SHA256 из release-manifest.json/SHA256SUMS; установка
  ТОЛЬКО вручную через hostd; авто-откат при сбое; `/var/lib/shmax`
  никогда не заменяется. `git pull` в продакшене — не поддерживается.
- Копии: локальный архив 0600 может быть незашифрованным и содержит мастер-ключ.
  Portable v2 экспортируется/скачивается из UI одним обязательно зашифрованным
  .shmax. Пароль задаёт владелец, не запрашивайте его для диагностики.
  После создания нового владельца onboarding предлагает восстановление вместо
  шагов 2–9. Hostd останавливает контейнер, мигрирует staging, меняет каталоги,
  проверяет readiness и откатывает неудачу. Старый production API in-process
  restore заблокирован; legacy CLI используйте только offline со safety-копией.

## Health-состояния

MAX/Telegram: HEALTHY / DEGRADED / DOWN / AUTH_REQUIRED (страница
«Диагностика»). Хост-здоровье: shmax-boot.service активен +
`docker port shmax-web-1` непуст + host curl `/health/ready` = 200.

## Типовые ошибки

| Симптом | Причина | Помощь |
|---|---|---|
| «Требуется установить 2FA» | 2FA выключена в MAX | включить в приложении MAX |
| SMS не идёт | задержка | ждать; не спамить resend |
| UI недоступен, контейнер healthy | публикация порта пуста (A11.1) | `docker compose up -d --force-recreate` в /opt/shmax |
| `/shmax_connect` не виден | сеть, код, права, слушатель; возможно Privacy Mode | проверить действующий код в General, права, /setprivacy→Disable и живой слушатель |
| «Требуется 2FA» при обновлении/входе | см. MAX 2FA | — |
| «источник обновлений не настроен» | нет SHMAX_UPDATE_REPO_URL | настроить env |
| «host helper unavailable» | hostd не запущен/нет сокета | проверить shmax-hostd.service |

## Безопасные диагностические команды

```bash
docker ps && docker compose -f /opt/shmax/compose.yaml ps
docker logs shmax-web-1 --tail 50
systemctl status shmax-boot.service shmax-hostd.service
journalctl -u shmax-boot -n 50
docker port shmax-web-1
curl -fsS 'http://<bind-address>:<port>/health/ready'
df -h /var/lib/shmax && free -h && ip -o addr
shmax version && shmax status && shmax doctor
```

## ЖЁСТКИЕ ПРАВИЛА ДЛЯ АССИСТЕНТА

1. НИКОГДА не просите пользователя вставить токен бота, пароли
   MAX, Telegram API Hash, Xray-подписку или что-либо секретное в чат/логи/issue.
2. НИКОГДА не предлагайте удалить `/var/lib/shmax` как «лечение».
3. `--purge` / `uninstall --purge` — только если пользователь
   ЯВНО сказал, что готов уничтожить данные.
4. Различайте контейнер-внутренний health и хост-доступность:
   проверяйте `docker port` и host curl.
5. Сначала диагностические (read-only) команды; предупреждайте
   перед командами, которые могут прервать мост/сеть/доступ.
6. Не выдумывайте setup-токен и не просите «перегенерировать» его
   на уже настроенной системе.
7. Не предлагайте `git pull` в продакшене — только релизы.
8. Помните: ПРИЛОЖЕНИЕ ОДНОРАЗОВО, ДАННЫЕ ПОСТОЯННЫ.

## Машинно-читаемые факты

```yaml
product: SHMAX
kind: self-hosted gateway
bridge: MAX(user account) <-> Telegram bot <-> forum supergroup
platform: [debian-13, odroid-xu4-armv7]
deployment_dir: /opt/shmax
data_dir: /var/lib/shmax
hostd_socket: /run/shmax/hostd.sock
web_port_default: 8787
wizard_steps: [admin, max, telegram-network, telegram-bot,
               telegram-group, max-chats, users, test, finish]
tg_transports: [direct, socks5, http, xray-subscription]
update_source: github-releases
channels: [stable, beta]
update_install: manual-only-via-hostd
persistence_invariant: app-disposable-data-persistent
setup_url: /setup?t=<one-time-token>
pairing_command: /shmax_connect <CODE>
```


## Синхронизация участников

Синхронизация Telegram обнаруживает молчавших участников через MTProto **бота**, а `chat_member` обновляет членство. Новые участники остаются не одобрены (`read_only`); Telegram admin не даёт SHMAX admin. API Hash хранится в SecretStore, сессия — только в памяти. Без необязательных настроек мост работает. Подробности, ограничения сети, backup и Privacy Mode: [Telegram-группа](ru/telegram-group.md#автоматическое-обнаружение-участников).

MAX всегда direct. Полная синхронизация требует необязательные API ID/API Hash
из my.telegram.org. Используется существующий BOT, без человеческого userbot,
OTP/QR/телефона в SHMAX. Молчавшим участникам не нужно писать боту. Ручная кнопка
«Синхронизировать с Telegram» — в Users; пропущенные настройки и ошибки не
останавливают мост. Обнаружение не равно авторизации. getMe не проверяет доставку
конкретной группы: предупреждение Privacy Mode означает лишь отсутствие
подтверждения отключения режима. Проверяйте права и живое сообщение.

## Чистая установка: hostd/boot

Установка требует полного архива, а не одиночного install.sh. Hostd — Python daemon
в отдельном `/opt/shmax-hostd` с минимальным venv для hostd/CLI/update/backup, без Node.
Сервис сам слушает `/run/shmax/hostd.sock` (root:shmax 0660, каталог 0750);
container path — `/run/shmax/hostd/hostd.sock` через read-only directory mount.
Socket activation unit не нужен. Boot-unit имеет конкретный ExecStart
`/opt/shmax/boot/shmax-boot.sh`, зависит от hostd и читает bind/port из `.env`.
`systemd-analyze verify` и проверка запуска/прав/доступа к сокету обязательны;
критический сбой не должен заканчиваться успешным баннером инсталлятора.

Локальная «Подпись в MAX» хранится только в существующем `telegram_users.nickname`. TG→MAX читает её из БД, затем использует Telegram-имя/@username. Один префикс для текста, ответов, caption и fallback; MAX→TG не меняется. Административный PATCH с CSRF, trim/clear, максимум 64 Unicode-символа без переносов и управляющих символов. Синхронизация участников никогда не меняет nickname или ACL.

## Перенос, медиа и безопасная помощь

- [Резервные копии](ru/backups.md), [восстановление](ru/restore.md), [медиа](ru/media.md).
- В portable входят БД/маршруты/ACL/подписи/member-sync настройки, SecretStore
  с ключами, config/state и доступные сеансы; кэш/логи/бинарники исключены.
  Временная MTProto bot-sync сессия не сохраняется: она пересоздаётся из настроек.
- Новый владелец сохраняет свой логин/пароль. Все старые веб-админы/сеансы
  удаляются, cookie signer меняется. Telegram-пользователи/одобрения сохраняются.
- Не переносите старый deployment IP. Новый .env/compose остаются на новом хосте.
  Остановите источник до запуска восстановленного моста: одинаковый бот/MAX
  аккаунт не должен одновременно обслуживаться двумя экземплярами.
- Потерянный пароль не восстанавливается. Неверный пароль/битый файл не меняют
  текущие данные. Не просите архив или секреты прислать в чат/логи/issue.
- При AUTH_REQUIRED восстановите только соответствующую интеграцию; не очищайте
  БД, Xray, маршруты или пользователей. При rollback_failed сохраните журнал и
  previous/; не удаляйте /var/lib/shmax и не советуйте purge.
- Фото используют реальный getFile/download через сохранённый transport.
  MEDIA_FALLBACK означает текстовый fallback, а не доставку вложения.
  Статические стикеры передаются изображением в MAX, native PNG в Telegram
  при допустимом размере. TGS/WebM используют thumbnail; без preview — текст.
- CPU-neutral SQLite/JSON формат рассчитан на amd64/ARMv7; реальный ARMv7,
  Telegram/MAX и перезагрузку Debian проверяет владелец при live acceptance.

## Навигация и незавершённая настройка
Создание администратора обязательно. После него доступны «Начать настройку», «Восстановить из резервной копии» и «Пропустить настройку». Пропуск открывает главную панель и сохраняется после обновления страницы и повторного входа. Он отмечает только `setup_dismissed`, а не `setup_complete`: MAX и Telegram остаются не настроенными, мост ожидает необходимых компонентов.

Нажмите блок SHMAX (включая значок и подпись) или «На главную» в мастере. «Отмена / Назад» также позволяет выйти из мастера. Несохранённые изменения требуют подтверждения; выход не сохраняет введённые значения, не отменяет MAX-авторизацию и не удаляет конфигурацию.

Настраивайте компоненты отдельно: MAX → «Настройки MAX» (`/max/settings`); Telegram → «Настройки сети», «Настройки бота», «Настройки группы». Эти страницы используют реальные существующие средства настройки без девяти шагов мастера. Авторизация MAX и «Проверить и сохранить» бота записывают результат явно; кнопка сохранения/возврата или отмены возвращает в раздел компонента. Пустой токен не заменяет сохранённый. На главной выберите «Продолжить первоначальную настройку», чтобы вернуться к шагу MAX, а не создавать администратора заново.

Истёкшая сессия MAX, неверный токен Telegram и недоступная сеть показываются в соответствующем разделе и не запускают мастер автоматически. Все разделы по-прежнему требуют входа администратора.


## Release v0.1.0 / Публикация

Debian 13 amd64: owner-confirmed live acceptance. ARMv7/ODROID-XU4: experimental; physical acceptance incomplete.
Текущий стабильный выпуск — v0.1.3. [Изменения](../release/v0.1.3/RELEASE_NOTES.ru.md), [процесс релиза](ru/release-process.md), [журнал событий](ru/event-journal.md). Публикация и production deploy выполняются только по явному разрешению владельца.
