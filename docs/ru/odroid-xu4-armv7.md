# ODROID-XU4 / ARMv7 (armhf)

**Экспериментальная цель. Физическая приёмка ARMv7/ODROID-XU4 не завершена. Живая приёмка подтверждена владельцем только для Debian 13 amd64.**

Целевая платформа: ODROID-XU4 (ARMv7, ~2 ГиБ RAM), Debian 13 /
Armbian.

## Пакеты сборки

zlib1g-dev, libjpeg62-turbo-dev, libpng-dev, libtiff-dev,
libwebp-dev, libfreetype6-dev, liblcms2-dev

## Пакеты рантайма

libjpeg62-turbo, libpng16-16t64, libtiff6, libwebp7,
libfreetype6, liblcms2-2, zlib1g

ВНИМАНИЕ: именно `libpng16-16t64` и `libtiff6`/`libwebp7` — не
выдуманные `libpng16-16`, `libtiff6t64` или `libwebp7t64`. Не запускайте
`apt autoremove` бездумно на Armbian — это сносил нужные пакеты.

## Замечания

- Сборка образа на ARMv7 медленная (компиляция Pillow и зависимостей)
  — это нормально, закладывайте время.
- Ограничение 2 ГиБ RAM: держите очередь небольшой;watchdog и
  poller рассчитаны на этот профиль.
