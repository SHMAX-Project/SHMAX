# Быстрый старт

Требования: Debian 13, root, Docker (установщик поставит сам при
отсутствии).

Из корня полного распакованного архива SHMAX:

```bash
sudo bash deploy/installer/install.sh --mode install
```

Инсталлятор:

1. создаст пользователя и каталоги (`/opt/shmax`, `/var/lib/shmax`);
2. поставит Docker при необходимости;
3. соберёт и запустит контейнер;
4. подождёт готовности `/health/ready`;
5. для **первой** установки напечатает одноразовую ссылку настройки
   и QR-код. Для upgrade/repair ссылка не требуется (A10).

Дальше откройте ссылку с телефона/ПК в LAN и пройдите мастер из
9 шагов (см. [first-setup.md](first-setup.md)).

Полезные команды сразу после установки:

```bash
systemctl status shmax-boot.service   # boot-оркестрация
docker compose -f /opt/shmax/compose.yaml ps
curl -fsS 'http://<bind-address>:<port>/health/ready'
```

Подставьте фактические bind/порт из `/opt/shmax/.env`. Для bind `0.0.0.0`
проверяйте `127.0.0.1`; для LAN bind — этот LAN-адрес.
