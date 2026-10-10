# Автоматическое восстановление Telegram

Сохранённый bot token, transport и proxy загружаются без повторного Save/onboarding.
Явный SOCKS5/HTTP не переходит на direct. AUTO рассматривает только настроенные
транспорты, сначала сохранённый рабочий вариант, затем прежний порядок fallback.

Startup пробует локальный proxy и authenticated getMe, устанавливает единственный
listener и проверяет receive path. Неудача не отменяет MAX и не блокирует web
навсегда. Один runtime-owned recovery task повторяет ограниченные попытки с jitter:
2, 4, 8… максимум 60 секунд между transient retries; healthy checks каждые 15 секунд.
Неверные credentials/configuration проверяются с 300-секундным cooldown, без сброса
token/подписки/сеанса. Shutdown отменяет task и закрывает sessions/poller.

Рабочий polling consumer не заменяется из-за отдельного failed health getMe.
Завершившийся consumer закрывается до установки следующего. Startup, настройки
и recovery используют один lifecycle lock; кандидат probe никогда не запускает polling.

Xray: installed executable, owned process, local SOCKS listening и authenticated
Telegram reachability — разные признаки. Живой слушающий proxy не перезапускается
из-за недоступности Telegram. Упавший managed Xray запускается из прежнего cached
selected node; три последовательные неудачи readiness живого процесса разрешают
его контролируемый restart с той же конфигурацией. Подписки/узлы не заменяются.

Статус `/api/status` включает `telegram.recovery`: state, attempts, sanitized failure,
retry_in_seconds. UI показывает recovery; `HEALTHY` требует работающего receive path
и актуальной проверки. `/api/telegram/xray/status` различает process_running,
socks_listening и telegram_reachable (последнее null без свежего evidence именно
через этот local proxy). GET status не стартует и не останавливает Xray.

Логи не содержат credentials. Если ошибка permanent, проверьте сохранённую настройку
локально; transient failure устраняется автоматически без повторного Save.
Процедура controlled deployment/rollback подготовлена отдельно для владельца;
никакое remote deployment не заявляется без SSH и acceptance evidence.
