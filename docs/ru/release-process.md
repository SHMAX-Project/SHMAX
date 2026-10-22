# Процесс релиза

Текущий стабильный выпуск — [v0.2.0](../../release/v0.2.0/RELEASE_NOTES.ru.md). Публикация и production deploy — отдельные действия по явному разрешению владельца. Исторические документы v0.1.0/v0.1.1 описывают состояние на момент тех выпусков; их теги и артефакты не заменяются.

1. Зафиксировать чистый public HEAD с согласованным `backend/shmax/VERSION`, Python/frontend metadata и документацией. Приватная история, серверные данные, backup и signing key не входят в public repository.
2. Выполнить `bash scripts/build-release.sh v0.2.0 OUTPUT`. Нужны Python 3.12+, Node >=20, npm, Git и npm registry/cache. Builder экспортирует committed HEAD, проверяет VERSION и выполняет npm ci/build в изолированном экспорте; working dist/node_modules не используются.
3. Проверить `python scripts/check-release.py OUTPUT`, состав source/production архивов и соответствие проверенному runtime. При изменении только документации подтвердить неизменность application files; изменение кода требует соответствующих тестов.
4. Подписать стандартным `python scripts/sign-release.py OUTPUT --key PRIVATE_KEY_PATH`. Ключ остаётся в доверенной локальной среде; существующий публичный verification key не заменяется.
5. Проверить backend/frontend/browser и изолированный Debian upgrade/rollback/bootstrap. Подготовить RU/EN notes, installer, public key, changelog и validation без private/server metadata.
6. После разрешения владельца опубликовать исходники, неизменяемый тег и draft Release, загрузить и проверить assets; сделать выпуск stable/latest. Сверить публичную загрузку, подпись, manifest и source commit. Production обновляется отдельно штатным updater, а не git pull.

Архивы: `shmax-v0.2.0-source.tar.gz` (без frontend/dist), `shmax-v0.2.0.tar.gz` (с frontend/dist), `SHA256SUMS` и подпись, `release-manifest.json`, `build-info.json`. Tar/Gzip timestamps и владельцы нормализованы по commit timestamp. Build-date sidecar находится вне архивов. Actions packaging проверяет сборку и сохраняет runner artifacts; сам не публикует Release.

[Runbook v0.2.0](../../release/v0.2.0/PUBLICATION_RUNBOOK.md) · [Тесты](testing.md) · [Обновления](updates.md).
