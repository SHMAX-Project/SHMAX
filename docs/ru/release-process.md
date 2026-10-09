# Процесс релиза

v0.1.0 подготовлен локально, не опубликован. Владелец должен разрешить публикацию
отдельной командой «А ТЕПЕРЬ ВЫКЛАДЫВАЕМ» после устранения блокеров аудита.

Из чистого committed HEAD: `bash scripts/build-release.sh v0.1.0 [output-directory]`.
Нужны Python 3.12+, Node >=20, npm, Git и доступ к npm registry/cache.
Builder экспортирует HEAD, проверяет VERSION, выполняет npm ci/build внутри
изолированного экспорта, а не копирует working frontend/dist. Никаких тегов/push.

Результат: `shmax-v0.1.0-source.tar.gz` (без frontend/dist),
`shmax-v0.1.0.tar.gz` (с frontend/dist и root compose.yaml), SHA256SUMS,
release-manifest.json (`{version, channel, artifact, sha256}`), build-info.json
(HEAD, дата, размеры, зависимости). Tar/Gzip timestamps и владельцы нормализованы
по commit timestamp; два прогона одного HEAD должны дать одинаковые архивы.
Build date в sidecar меняется и не входит в архив. Manifest совместим с updater.

Проверить `python scripts/check-release.py <output-directory>`; извлечь архив вне
checkout и проверить import/version, executable installer, наличие frontend/dist.
Пересобрать после любого изменения HEAD. Actions packaging проверяет и сохраняет
runner artifacts, не создаёт GitHub Release. CI ещё не выполнялся на GitHub.

[Точный будущий runbook](../../release/v0.1.0/PUBLICATION_RUNBOOK.md) ·
[Статус локальной подготовки](../../release/v0.1.0/PUBLICATION_STATUS.md).
Обновление production — через UI/hostd и проверенный релиз, не git pull.
