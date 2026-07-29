#!/usr/bin/env bash
#
# Настроить автоматическую выкладку стенда из GitHub Actions.
# Выполняется на сервере ОДИН раз, после deploy/install.sh.
#
#   sudo bash deploy/setup-ci-deploy.sh itchamp.root72.ru
#
# Что делает:
#   • заводит пользователя ktkdeploy — только для выкладки, без пароля;
#   • разрешает ему через sudo ровно одну команду: /usr/local/sbin/ktk-update;
#   • пишет сам ktk-update — обновление кода и перезапуск стека;
#   • генерирует ключ SSH и печатает, что вставить в секреты репозитория.
#
# Почему пользователь отдельный, а не root: ключ будет лежать в секретах
# GitHub, и утечка не должна означать полный доступ к серверу. Через sudo
# разрешена одна команда без аргументов — больше сделать нечем.
#
# Почему ktk-update лежит в /usr/local/sbin, а не в репозитории: выкладка
# делает `git reset --hard` и переписывает файлы репозитория. Bash читает
# скрипт по мере выполнения, и подмена файла на середине ломает запуск —
# скрипт обязан лежать там, куда выкладка не дотягивается.
#
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR="${APP_DIR:-/opt/ktk}"
BRANCH="${BRANCH:-claude/roles-auth-training-review-i9u90o}"
DEPLOY_USER=ktkdeploy
UPDATE_BIN=/usr/local/sbin/ktk-update

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запускать от root: sudo bash deploy/setup-ci-deploy.sh <домен>"
[[ -n "$DOMAIN" ]] || die "Не указан домен"
[[ -d "$APP_DIR/.git" ]] || die "Нет $APP_DIR — сначала выполните deploy/install.sh"

# ------------------------------------------------------------- пользователь
if ! id "$DEPLOY_USER" &>/dev/null; then
    say "Завожу пользователя $DEPLOY_USER"
    adduser --system --group --shell /bin/bash \
            --home "/home/$DEPLOY_USER" "$DEPLOY_USER"
fi
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"

# ------------------------------------------------------- команда обновления
say "Ставлю $UPDATE_BIN"
cat > "$UPDATE_BIN" <<EOF
#!/usr/bin/env bash
# Обновление стенда КТК ЭЛОУ-АВТ. Вызывается из GitHub Actions по SSH.
# Создан deploy/setup-ci-deploy.sh — правки здесь переживают выкладку,
# в отличие от файлов репозитория.
set -euo pipefail

APP_DIR=$APP_DIR
BRANCH=$BRANCH
DOMAIN=$DOMAIN
COMPOSE="\$APP_DIR/deploy/docker-compose.prod.yml"

echo "== забираю \$BRANCH"
git -C "\$APP_DIR" fetch --depth 1 origin "\$BRANCH"
BEFORE=\$(git -C "\$APP_DIR" rev-parse HEAD)
git -C "\$APP_DIR" reset --hard "origin/\$BRANCH"
AFTER=\$(git -C "\$APP_DIR" rev-parse HEAD)
echo "   \${BEFORE:0:7} -> \${AFTER:0:7}"

echo "== пересобираю и поднимаю"
# Слои кэшируются: если requirements.txt не менялся, пересобирается только
# копирование кода — на одном ядре это десятки секунд, а не минуты.
docker compose -f "\$COMPOSE" up -d --build --remove-orphans

echo "== проверяю"
PORT=\$(grep -oP '(?<=^APP_PORT=).*' "\$APP_DIR/deploy/.env" 2>/dev/null || echo 8000)
for _ in \$(seq 1 45); do
    code=\$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:\$PORT/login" || true)
    [[ "\$code" == "200" ]] && break
    sleep 2
done
if [[ "\$code" != "200" ]]; then
    echo "!! стенд не ответил (код \$code), откатываюсь на \${BEFORE:0:7}"
    git -C "\$APP_DIR" reset --hard "\$BEFORE"
    docker compose -f "\$COMPOSE" up -d --build
    exit 1
fi

# лишние слои копятся с каждой сборкой, а диска 15 ГБ
docker image prune -f >/dev/null || true
echo "== готово: \$DOMAIN обновлён до \${AFTER:0:7}"
EOF
chmod 750 "$UPDATE_BIN"

# ------------------------------------------------------------------- sudo
say "Разрешаю $DEPLOY_USER одну команду через sudo"
cat > /etc/sudoers.d/ktk-deploy <<EOF
# Выкладка стенда КТК из GitHub Actions. Ровно одна команда, без аргументов:
# всё остальное на сервере этому пользователю недоступно.
$DEPLOY_USER ALL=(root) NOPASSWD: $UPDATE_BIN
EOF
chmod 440 /etc/sudoers.d/ktk-deploy
visudo -c -f /etc/sudoers.d/ktk-deploy >/dev/null || die "sudoers не прошёл проверку"

# -------------------------------------------------------------------- ключ
KEY=/root/.ktk-deploy-key
if [[ ! -f "$KEY" ]]; then
    say "Генерирую ключ SSH для выкладки"
    ssh-keygen -t ed25519 -N '' -C "github-actions-ktk-deploy" -f "$KEY" >/dev/null
fi
install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
        "$KEY.pub" "/home/$DEPLOY_USER/.ssh/authorized_keys"

say "Проверяю вход по ключу"
# Именно так будет заходить GitHub Actions. Проверяем здесь, иначе ошибка
# всплывёт в чужом окружении, где её неудобно разбирать.
ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 \
    "$DEPLOY_USER@127.0.0.1" true 2>/dev/null \
    || die "Вход по ключу не работает. Проверьте, что sshd разрешает
   вход пользователю $DEPLOY_USER (AllowUsers/DenyUsers в /etc/ssh/sshd_config)
   и что аутентификация по ключу включена (PubkeyAuthentication yes)."

say "Пробная выкладка (пересборка образа — несколько минут)"
sudo -u "$DEPLOY_USER" sudo -n "$UPDATE_BIN" \
    || die "Пробный запуск $UPDATE_BIN не удался — смотрите вывод выше"

SSH_PORT=$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}' || echo 22)
HOST_IP=$(curl -s --max-time 10 https://api.ipify.org || echo "<адрес сервера>")

printf '\n\033[1;32m=== Готово. Осталось добавить секреты в GitHub ===\033[0m\n\n'
cat <<EOF
Репозиторий → Settings → Secrets and variables → Actions → New repository secret.
Три штуки, имена важны:

--- DEPLOY_HOST ---------------------------------------------------------
$HOST_IP
--- DEPLOY_PORT ---------------------------------------------------------
$SSH_PORT
--- DEPLOY_SSH_KEY (скопировать целиком, вместе со строками BEGIN и END) --
EOF
cat "$KEY"
cat <<EOF
-------------------------------------------------------------------------

После этого каждый push в ветку $BRANCH будет:
  прогонять тесты на SQLite и PostgreSQL, собирать образ,
  и только при успехе обновлять $DOMAIN.

Проверить руками, без GitHub:
  sudo $UPDATE_BIN

Отозвать доступ, если ключ утёк:
  sudo rm /home/$DEPLOY_USER/.ssh/authorized_keys
EOF
