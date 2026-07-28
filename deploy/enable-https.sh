#!/usr/bin/env bash
#
# Выпуск сертификата Let's Encrypt и перевод стенда на HTTPS.
#
#   sudo bash deploy/enable-https.sh itchamp.root72.ru
#
# Запускать после install.sh и только когда домен уже указывает на этот
# сервер: Let's Encrypt проверяет владение доменом, обращаясь по нему.
#
# Отдельным шагом, а не внутри install.sh, ровно поэтому: до появления
# сертификата включать `KTK_COOKIE_SECURE=1` нельзя — браузер перестанет
# отдавать cookie сеанса по http, и вход сломается.
#
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR="${APP_DIR:-/opt/ktk}"
ENV_FILE="$APP_DIR/deploy/.env"
COMPOSE_FILE="$APP_DIR/deploy/docker-compose.prod.yml"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запускать от root: sudo bash deploy/enable-https.sh <домен>"
[[ -n "$DOMAIN" ]] || die "Не указан домен"
[[ -f "$ENV_FILE" ]] || die "Нет $ENV_FILE — сначала выполните deploy/install.sh"

say "Проверяю, что домен указывает на этот сервер"
resolved=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)
myip=$(curl -s --max-time 10 https://api.ipify.org || true)
if [[ -n "$resolved" && -n "$myip" && "$resolved" != "$myip" ]]; then
    printf '\033[1;33m!!  %s\033[0m\n' \
        "$DOMAIN указывает на $resolved, а внешний адрес сервера $myip."
    printf '    Если DNS ещё не разошёлся — подождите и повторите.\n'
    read -r -p "    Всё равно продолжить? [y/N] " ans
    [[ "$ans" == [yY] ]] || exit 1
fi

say "Ставлю certbot"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq certbot python3-certbot-nginx

say "Выпускаю сертификат"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        --register-unsafely-without-email --redirect

# Теперь сайт доступен по https — можно требовать защищённую cookie.
say "Включаю передачу cookie сеанса только по HTTPS"
sed -i 's/^KTK_COOKIE_SECURE=.*/KTK_COOKIE_SECURE=1/' "$ENV_FILE"
docker compose -f "$COMPOSE_FILE" up -d

say "Проверяю"
sleep 2
code=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/login" || true)
[[ "$code" == "200" ]] || die "https://$DOMAIN/login ответил $code. Журнал: docker compose -f $COMPOSE_FILE logs --tail 40 app"

printf '\n\033[1;32m=== Готово ===\033[0m\n'
echo "Открыть: https://$DOMAIN"
echo "Сертификат продлевается сам (таймер systemd certbot.timer)."
