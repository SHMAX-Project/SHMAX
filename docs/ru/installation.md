# Установка и режимы инсталлятора

Инсталлятор: `deploy/installer/install.sh`. Общий флаг
`--dry-run` — показать план без изменений.

## Режимы

| Режим | Что делает | Setup-токен |
|---|---|---|
| `--mode install` | полная установка: пользователь, каталоги, Docker, контейнер, boot-unit | только если настройка ещё не завершена |
| `--mode repair`  | повторно раскладывает файлы, **данные сохраняются** | никогда |
| `--mode upgrade` | обновляет артефакты деплоя, **данные сохраняются** | никогда |
| `--mode uninstall` | удаляет приложение, **оставляет данные** (см. `--purge`) | — |

Важно: после успешного upgrade инсталлятор НЕ требует setup-токен
и не завершается с ошибкой (live-баг A10 исправлен).

## Каталоги

- `/opt/shmax` — деплой: `compose.yaml`, `install.sh`, `boot/`.
- `/var/lib/shmax` — постоянные данные. Никогда не удаляется без
  явного `--purge`.

## Debian 13 / требования

- systemd (unit `shmax-boot.service` ставится автоматически);
- Docker Engine + Compose v2 (ставится при отсутствии);
- для ARMv7 см. [odroid-xu4-armv7.md](odroid-xu4-armv7.md).

## Ручной офлайн-апгрейд

Рекомендуемый путь обновления — через UI (см.
[updates.md](updates.md)). Ручная замена архива релиза возможна,
но не поддерживается как основной сценарий: апдейтер делает то же
самое + проверки + откат.

## Hostd и systemd после чистой установки

Запускайте `sudo bash deploy/installer/install.sh --mode install` из полного
распакованного архива SHMAX. Один отдельно скачанный install.sh недостаточен:
установщику нужны backend, Dockerfile, compose, boot и systemd-файлы.

Установщик создаёт отдельный root-owned runtime `/opt/shmax-hostd`:
Python >=3.12 venv, копия Python-пакета и только зависимости hostd/CLI/backup/update.
Node и checkout разработчика на хосте не нужны. Runtime сохраняется при замене
`/opt/shmax`; repair/upgrade обновляют его идемпотентно.

`shmax-hostd.service` сам создаёт `/run/shmax/hostd.sock`; socket-unit не нужен.
Каталог — root:shmax 0750, сокет — root:shmax 0660. Сервис сохраняет каталог при
рестарте, удаляет/пересоздаёт сокет и отказывается заменять обычный файл.
Контейнер получает read-only mount этого каталога в `/run/shmax/hostd` и обращается
к `/run/shmax/hostd/hostd.sock`; Docker socket в контейнер не передаётся.

Boot-unit устанавливается без подстановок: `ExecStart=/opt/shmax/boot/shmax-boot.sh`.
Оба unit проверяются `systemd-analyze verify` перед enable/start; ошибка проверки,
запуска hostd, появления сокета или его прав прерывает установку. Также проверяется
connect к сокету с UID/GID контейнера (по умолчанию 8787:8787).

Uninstall отключает и удаляет оба unit, runtime hostd и runtime-каталог;
`/var/lib/shmax` сохраняется. Данные удаляет только явно подтверждённый `--purge`.

## Установка v0.1.0 из релизного архива

Публичный URL ещё не назначен. После публикации скачайте production архив,
SHA256SUMS и build-info.json с утверждённого release page. На Debian:

```bash
sha256sum -c SHA256SUMS --ignore-missing
tar -xzf shmax-v0.1.0.tar.gz
cd shmax-v0.1.0
# Сначала изучите deploy/installer/install.sh.
sudo bash deploy/installer/install.sh --mode install
```

Полный исходный архив также содержит Dockerfile/frontend sources/lock;
production архив дополнительно содержит собранный frontend/dist. При локальной
сборке Docker всё равно получает зависимости/base images/Xray из сети: наличие
архива не означает полностью автономную установку. Требуется Debian 13 amd64,
root/sudo, Docker/Compose v2 и systemd; installer устанавливает недостающие средства.
ARMv7 — экспериментальный target без завершённой физической приёмки.

```bash
sudo bash deploy/installer/install.sh --mode repair
sudo bash deploy/installer/install.sh --mode upgrade
sudo bash deploy/installer/install.sh --mode uninstall
```

Repair/upgrade сохраняют `/var/lib/shmax`; uninstall также сохраняет данные.
`--purge` удаляет данные и требует отдельного осознанного подтверждения; не
добавляйте его для обычного удаления приложения. Перед upgrade/uninstall
сделайте [копию](backups.md); [восстановление](restore.md) через UI использует
portable encrypted export, не обычную локальную копию.

Не выполняйте `curl | sudo bash` для неизвестного URL. Загрузите полный архив,
проверьте SHA из доверенного канала, изучите скрипт; хеш с того же скомпрометированного
сайта сам по себе не подтверждает подлинность. Рабочий one-line URL пока не существует.
