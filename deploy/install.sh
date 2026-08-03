#!/usr/bin/env bash
#
# Развёртывание КТК ЭЛОУ-АВТ на сервере с Docker и nginx.
# Рассчитано на небольшую машину: 1 ядро, 1 ГБ ОЗУ.
#
#   sudo bash deploy/install.sh itchamp.root72.ru
#
# Что делает:
#   • при нехватке подкачки добавляет файл на 1 ГБ (сборка образа на 1 ГБ
#     ОЗУ иначе может упереться в OOM);
#   • генерирует пароли БД, ключ подписи токенов и пароли учебных записей;
#   • поднимает стек deploy/docker-compose.prod.yml — приложение и PostgreSQL,
#     без pgAdmin и без открытых наружу портов;
#   • настраивает сайт nginx с поддержкой WebSocket.
#
# Идемпотентен: повторный запуск обновляет код и пересобирает образ, не трогая
# уже созданные пароли и базу. Его же используйте для выкладки обновлений.
#
# TLS — вторым шагом, когда домен уже указывает на сервер:
#   sudo bash deploy/enable-https.sh itchamp.root72.ru
#
set -euo pipefail

DOMAIN="${1:-}"
REPO_URL="${REPO_URL:-https://github.com/mranton152/itchamp.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/ktk}"
# На сервере может уже что-то слушать 8000 — порт можно переопределить:
#   APP_PORT=8010 sudo -E bash deploy/install.sh <домен>
APP_PORT="${APP_PORT:-8000}"

COMPOSE_DIR="$APP_DIR/deploy"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.prod.yml"
ENV_FILE="$COMPOSE_DIR/.env"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запускать от root: sudo bash deploy/install.sh <домен>"
[[ -n "$DOMAIN" ]] || die "Не указан домен. Пример: sudo bash deploy/install.sh itchamp.root72.ru"
command -v docker >/dev/null || die "Docker не найден"
docker compose version >/dev/null 2>&1 || die "Нужен плагин docker compose (v2)"

# ---------------------------------------------------------------- подкачка
if [[ $(free -m | awk '/^Swap:/ {print $2}') -eq 0 ]]; then
    say "Добавляю файл подкачки на 1 ГБ"
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
    say "Подкачка уже есть — пропускаю"
fi

command -v nginx >/dev/null || {
    say "Ставлю nginx"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx
}

# --------------------------------------------------------------------- код
say "Забираю код: ветка $BRANCH"
if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" remote set-url origin "$REPO_URL"
    git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"
    git -C "$APP_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
    git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
    mkdir -p "$(dirname "$APP_DIR")"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR" || die \
"Не удалось склонировать $REPO_URL.
   Если репозиторий закрытый, укажите ссылку с токеном:
   REPO_URL='https://<токен>@github.com/mranton152/itchamp.git' sudo -E bash deploy/install.sh $DOMAIN"
fi

# --------------------------------------------------------------- настройки
if [[ -f "$ENV_FILE" ]]; then
    say "Настройки уже есть — пароли из $ENV_FILE сохраняю"
    NEW_INSTALL=0
else
    say "Генерирую ключ подписи и пароли"
    NEW_INSTALL=1
    # tr -dc после base64: из пароля убираются / + =, чтобы он без экранирования
    # ложился и в строку подключения к БД, и в .env
    PW_OPERATOR=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-14)
    PW_INSTRUCTOR=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-14)
    PW_ADMIN=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-14)
    cat > "$ENV_FILE" <<EOF
# Секреты стенда КТК ЭЛОУ-АВТ. Создан $(date '+%Y-%m-%d %H:%M').
# В git не попадает (см. .gitignore). Читает только root.
POSTGRES_PASSWORD=$(openssl rand -hex 16)
KTK_SECRET_KEY=$(openssl rand -hex 32)

# Переключается скриптом enable-https.sh после выпуска сертификата.
KTK_COOKIE_SECURE=0

# Пароли учебных записей. Применились при первом запуске на пустой базе;
# менять их теперь нужно в разделе /admin — правка этого файла не поможет.
KTK_PASSWORD_OPERATOR=$PW_OPERATOR
KTK_PASSWORD_INSTRUCTOR=$PW_INSTRUCTOR
KTK_PASSWORD_ADMIN=$PW_ADMIN
EOF
fi
chmod 600 "$ENV_FILE"

# ------------------------------------------------------------------- стек
# Сервер может быть не пустым: занятый порт лучше поймать здесь, чем получить
# невнятную ошибку docker при публикации.
if ss -ltnH "sport = :$APP_PORT" 2>/dev/null | grep -q . \
   && ! docker ps --format '{{.Names}} {{.Ports}}' | grep -q "^ktk-trainer.*:$APP_PORT"; then
    die "Порт $APP_PORT на 127.0.0.1 уже занят другим процессом.
   Запустите с другим портом: APP_PORT=8010 sudo -E bash deploy/install.sh $DOMAIN"
fi
grep -q '^APP_PORT=' "$ENV_FILE" && sed -i "s/^APP_PORT=.*/APP_PORT=$APP_PORT/" "$ENV_FILE" \
    || echo "APP_PORT=$APP_PORT" >> "$ENV_FILE"

say "Собираю образ и поднимаю стек (первая сборка — несколько минут)"
docker compose -f "$COMPOSE_FILE" up -d --build --remove-orphans

# Корневой docker-compose.yml — для разработки, он публикует порты наружу
# и тянет pgAdmin. Если его когда-то поднимали здесь же, гасим.
if docker ps --format '{{.Names}}' | grep -q '^ktk-pgadmin$'; then
    warn "Останавливаю pgAdmin: на 1 ГБ ОЗУ ему не место"
    docker rm -f ktk-pgadmin >/dev/null
fi

# ------------------------------------------------------------------ nginx
say "Настраиваю nginx для $DOMAIN"
cat > /etc/nginx/conf.d/ktk-upgrade.conf <<'EOF'
# Заголовок Connection для рукопожатия WebSocket. Директива map допустима
# только в контексте http, поэтому лежит отдельно от файла сайта.
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
EOF
sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$APP_PORT/g" \
    "$COMPOSE_DIR/nginx-ktk.conf" > /etc/nginx/sites-available/ktk
ln -sf /etc/nginx/sites-available/ktk /etc/nginx/sites-enabled/ktk
# Дефолтный сайт намеренно не трогаем: сервер не пустой, и он может
# обслуживать чужие домены. Наш блок выбирается по server_name.
nginx -t
systemctl reload nginx

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    say "Открываю 80/443 в ufw"
    ufw allow 'Nginx Full' >/dev/null
fi

# --------------------------------------------------------------- проверка
say "Проверяю, что тренажёр отвечает"
code=000
for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$APP_PORT/login" || true)
    [[ "$code" == "200" ]] && break
    sleep 2
done
[[ "$code" == "200" ]] || {
    warn "Тренажёр не ответил (код $code). Журнал контейнера:"
    docker compose -f "$COMPOSE_FILE" logs --tail 40 app
    exit 1
}

printf '\n\033[1;32m=== Готово ===\033[0m\n'
echo "Открыть: http://$DOMAIN"
echo
if [[ "$NEW_INSTALL" == "1" ]]; then
    echo "Учётные записи (пароли показываются один раз):"
    printf '  %-11s %s\n' operator "$PW_OPERATOR" instructor "$PW_INSTRUCTOR" admin "$PW_ADMIN"
    echo
    echo "Они же лежат в $ENV_FILE"
else
    echo "Пароли не менялись: см. $ENV_FILE или раздел /admin."
fi
cat <<EOF

Следующий шаг — HTTPS (домен уже должен указывать на этот сервер):
  sudo bash $COMPOSE_DIR/enable-https.sh $DOMAIN

Полезное:
  docker compose -f $COMPOSE_FILE ps
  docker compose -f $COMPOSE_FILE logs -f app
  sudo bash $COMPOSE_DIR/install.sh $DOMAIN     — обновить код и пересобрать
EOF
