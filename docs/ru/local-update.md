# Обновление локальным production-пакетом

Обычная команда после установки нового updater:

```bash
sudo shmax update /path/to/shmax-v0.2.1.tar.gz
```

Старый CLI принимал только `update check` и `update apply`. Для первого перехода
вместе с пакетом поставляется самостоятельный executable `shmax`, использующий
системный Python и стандартную библиотеку. Выполните одну команду с полным путём:

```bash
sudo /path/to/shmax update /path/to/shmax-v0.2.1.tar.gz
```

После успешного обновления он автоматически устанавливает обработчик обычной
команды `sudo shmax update ...`. Существующие команды направляются прежнему
host runtime. Это явный запуск владельцем; подготовка пакета не меняет сервер.

Updater проверяет inventory/SHA256 внутри production archive, безопасные пути,
комплектность, место для backup/build/rollback и сериализует обновления lock.
Перед обновлением из локального файла проверьте подпись и SHA-256 из официального
выпуска по [инструкции установки](installation.md#ручная-установка-из-архива).
Внутренний список контрольных сумм проверяет целостность файлов архива.

Новый image строится под отдельным tag до остановки существующего. Далее
объявляется короткий перерыв MAX/Telegram, web останавливается; полный cold
backup сохраняет DB, SecretStore, sessions, настройки, ownership/permissions.
Извлечённая backup проверяется по bytes/ownership и SQLite integrity/foreign keys.
Старый exact image ID сохраняется. Не выполняется чистая установка или сброс auth.

Acceptance требует опубликованный `/health/ready`, ready DB/storage/runtime,
Docker healthy и свежий private deployment-readiness report текущего запуска.
Для настроенных интеграций нужны connected/authenticated MAX с listener и
Telegram с активным polling; отсутствие конфигурации явно фиксируется отдельно.
Никаких test messages, второго getUpdates consumer или ручного редактирования DB.

При ошибке до rollback сохраняются protected container state, exit status,
entrypoint, sanitized traceback/log и command stages в
`/var/backups/shmax/update-<id>` (directory 0700). Затем запускается прежний
образ без пересборки. Если холодная копия прошла проверку, updater перед запуском
проверяет её контрольную сумму и восстанавливает данные из копии. Затем проверяется
готовность прежнего приложения.
Если предыдущий image также не готов, ошибка говорит UPDATE AND ROLLBACK FAILED.

`.env` сохраняется с исходными owner/mode; меняются только image/deployment ID
после успешной приёмки. Bind адрес не меняется. Исходники нового пакета остаются
в `/opt/shmax/releases/<id>`; running container не патчится вручную.
