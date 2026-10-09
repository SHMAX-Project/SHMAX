# Журналы

- Файл: `/var/lib/shmax/logs/shmax.log` (ротация включена).
- CLI: `shmax logs --tail 100`.
- Docker: `docker logs shmax-web-1 --tail 100`.
- В UI: страница «Журнал» (хвост файла).

Секреты в логи не пишутся: токены маскируются, URL подписки и
VLESS-ссылки не логируются. Если увидели секрет в логе — смените
его и сообщите в Security Advisories.
