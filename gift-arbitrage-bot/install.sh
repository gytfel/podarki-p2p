#!/usr/bin/env bash
# Установка и обновление бота на VPS с Ubuntu 22.04+ или Debian 12+:
#
#   sudo bash install.sh
#   sudo bash install.sh diag   — обновить код и снять ответы площадок с этого сервера (поиск причины ошибок)
#
# Первый запуск ставит Python, создаёт пользователя giftbot, копирует бота в /opt/gift-arbitrage-bot,
# спрашивает токены, узнаёт ваш chat_id, входит в Telegram-аккаунт, проверяет площадки и включает
# автозапуск. Повторный запуск обновляет код и зависимости, а настройки (.env) и папку data/ не трогает.
set -euo pipefail
MODE="${1:-install}"
case $MODE in
  install | diag) ;;
  *) printf 'Неизвестный режим «%s». Можно: bash install.sh или bash install.sh diag\n' "$MODE" >&2; exit 2 ;;
esac
umask 022  # при строгой umask пользователь giftbot не смог бы прочитать код и библиотеки
# «su» без минуса оставляет PATH без /usr/sbin, где лежат useradd и runuser
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

APP_DIR="${APP_DIR:-/opt/gift-arbitrage-bot}"
APP_USER="${APP_USER:-giftbot}"
SERVICE="${SERVICE:-giftbot}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"
UNIT_FILE="/etc/systemd/system/$SERVICE.service"
PY="$APP_DIR/.venv/bin/python"

TOKEN_RE='^[0-9]{5,}:[A-Za-z0-9_-]{30,}$'
API_ID_RE='^[0-9]{3,}$'
API_HASH_RE='^[0-9a-fA-F]{32}$'

# В подсказках пишем sudo, только если скрипт запущен через sudo: на Debian под root его может не быть
SUDO=""
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then SUDO="sudo "; fi

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
has_systemd() { [ -d /run/systemd/system ]; }

# Ctrl+C, обрыв SSH, kill — это ошибка, а не успех: тогда сработает on_exit
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
restart_on_abort=0  # 1 — бот работал до обновления: если установка прервётся, запускаем его обратно
on_exit() {
  local code=$?
  if [ "$code" -ne 0 ] && [ "$restart_on_abort" = 1 ]; then
    if systemctl start "$SERVICE" 2>/dev/null; then
      warn "Установка не завершена — бот снова запущен"
    else
      warn "Установка не завершена, бот сейчас не работает. Запустить: ${SUDO}systemctl start $SERVICE"
    fi
  fi
}
trap on_exit EXIT

# Бот работает от пользователя giftbot из папки установки и с чистым окружением, как под systemd:
# если проверка прошла здесь, служба увидит то же самое
run_bot() {
  (cd "$APP_DIR" && runuser -u "$APP_USER" -- env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME="$APP_DIR" LANG=C.UTF-8 TERM="${TERM:-dumb}" PYTHONUNBUFFERED=1 "$PY" -m giftbot "$@")
}

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
env_set() {
  local escaped
  escaped=$(printf '%s' "$2" | sed 's/[\\&|]/\\&/g')
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$escaped|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}
# ask КЛЮЧ "вопрос" 'регулярка' — спрашивает, только если в .env значения ещё нет
ask() {
  local key=$1 prompt=$2 pattern=$3 value
  [ -n "$(env_get "$key")" ] && return 0
  while true; do
    read -r -p "$prompt: " value || die "Ввод прерван — запустите install.sh ещё раз"
    value=$(printf '%s' "$value" | tr -d '[:space:]')
    [[ $value =~ $pattern ]] && break
    warn "Не похоже на правильное значение, попробуйте ещё раз"
  done
  env_set "$key" "$value"
}
reask() { env_set "$1" ""; ask "$@"; }

# На только что созданном сервере apt первые минуты занят автообновлением
apt_install() {
  local i err
  export DEBIAN_FRONTEND=noninteractive
  for i in $(seq 1 60); do
    if err=$(apt-get update -qq 2>&1); then break; fi
    if [ "$i" -eq 60 ]; then
      printf '%s\n' "$err" >&2
      die "apt-get update не прошёл — проверьте интернет на сервере и запустите install.sh ещё раз"
    fi
    if [ "$i" -eq 1 ]; then
      warn "apt пока недоступен (на новом сервере он занят автообновлением) — повторяю, это до 10 минут…"
    fi
    sleep 10
  done
  apt-get -o DPkg::Lock::Timeout=600 install -y -qq "$@" >/dev/null
}

[ "$(id -u)" -eq 0 ] || die "Запустите от root: sudo bash $SRC_DIR/install.sh (если sudo нет — сначала su -)"
command -v apt-get >/dev/null || die "Скрипт рассчитан на Ubuntu или Debian (нужен apt-get)"
[ -f "$SRC_DIR/giftbot/__main__.py" ] || die "Рядом со скриптом нет бота: запускайте install.sh из папки gift-arbitrage-bot"

# ------------------------------------------------------------------ система

step "Системные пакеты"
packages=()
if ! command -v python3 >/dev/null || ! python3 -c 'import ensurepip' 2>/dev/null; then
  packages+=(python3 python3-venv)
fi
command -v nano >/dev/null || packages+=(nano)  # редактор для настроек, в минимальных образах его нет
if [ ${#packages[@]} -gt 0 ]; then
  apt_install "${packages[@]}"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "Нужен Python 3.10 или новее, в системе $(python3 -V 2>&1). Возьмите VPS с Ubuntu 22.04/24.04 или Debian 12"
ok "$(python3 -V)"

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  ok "Создан пользователь $APP_USER"
fi

# ------------------------------------------------------------------ код и зависимости

step "Копирую бота в $APP_DIR"
if has_systemd && [ -f "$UNIT_FILE" ]; then
  # останавливаем и бота, который перезапускается после сбоя: две копии не должны делить одну сессию
  if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then restart_on_abort=1; fi
  systemctl stop "$SERVICE" || true
  ok "Бот остановлен на время обновления"
fi
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  rm -rf "$APP_DIR/giftbot" "$APP_DIR/tests"
  tar -C "$SRC_DIR" --exclude=./.env --exclude=./data --exclude=./.venv --exclude=./.git --exclude=__pycache__ -cf - . \
    | tar -C "$APP_DIR" --no-same-owner --no-same-permissions -xf -
fi
ok "Код на месте"

step "Зависимости Python (1–3 минуты)"
# venv без pip остаётся, если создание прервали — такой пересоздаём
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || ! "$PY" -m pip --version >/dev/null 2>&1; then
  rm -rf "$APP_DIR/.venv"
  python3 -m venv "$APP_DIR/.venv"
fi
"$PY" -m pip install -q --disable-pip-version-check --upgrade pip
"$PY" -m pip install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
# код и библиотеки должен читать пользователь бота, даже если их поставили со строгой umask
chmod 755 "$APP_DIR"
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name data ! -name .env -exec chmod -R a+rX {} +
ok "Зависимости установлены"

# ------------------------------------------------------------------ настройки

step "Настройки"
if [ ! -f "$ENV_FILE" ]; then
  install -m 640 -o root -g "$APP_USER" "$APP_DIR/.env.example" "$ENV_FILE"
  ok "Создан $ENV_FILE"
fi
# .env читает только бот, менять может только root: в нём ключи от вашего аккаунта
chown "root:$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"
data_dir=$(env_get DATA_DIR)
data_dir=${data_dir:-data}
[[ $data_dir = /* ]] || data_dir="$APP_DIR/$data_dir"
mkdir -p "$data_dir"
chown -R "$APP_USER:$APP_USER" "$data_dir"
chmod 700 "$data_dir"

# флоры Tonnel теперь берём с gifts2, как веб-приложение Tonnel; прежнее значение по умолчанию переносим
sed -i 's#^TONNEL_STATS_URL=https://gifts3\.tonnel\.network/api/filterStats$#TONNEL_STATS_URL=https://gifts2.tonnel.network/api/filterStats#' "$ENV_FILE"

ask BOT_TOKEN "Токен бота от @BotFather (вида 123456:AA...)" "$TOKEN_RE"
ask API_ID "API_ID с my.telegram.org (число)" "$API_ID_RE"
ask API_HASH "API_HASH с my.telegram.org (32 символа)" "$API_HASH_RE"

if [ -z "$(env_get ADMIN_CHAT_ID)" ]; then
  step "Узнаю ваш chat_id"
  while [ -z "$(env_get ADMIN_CHAT_ID)" ]; do
    echo "Откройте своего бота в Telegram и отправьте ему /start (жду 2 минуты)."
    out=$(mktemp)
    run_bot chatid | tee "$out" || true
    chat_id=$(sed -n 's/.*chat_id: \(-\{0,1\}[0-9]\{1,\}\).*/\1/p' "$out" | tail -n 1)
    bad_token=0
    if grep -qE 'Unauthorized|Not Found' "$out"; then bad_token=1; fi
    rm -f "$out"
    if [ -n "$chat_id" ]; then
      env_set ADMIN_CHAT_ID "$chat_id"
      ok "ADMIN_CHAT_ID=$chat_id"
    elif [ "$bad_token" = 1 ]; then
      warn "Telegram не принял токен бота — введите его ещё раз"
      reask BOT_TOKEN "Токен бота от @BotFather (вида 123456:AA...)" "$TOKEN_RE"
    else
      read -r -p "Сообщение не пришло. Enter — подождать ещё раз, или введите chat_id числом: " answer \
        || die "Ввод прерван — запустите install.sh ещё раз"
      answer=$(printf '%s' "$answer" | tr -d '[:space:]')
      if [[ $answer =~ ^-?[0-9]+$ ]]; then env_set ADMIN_CHAT_ID "$answer"; fi
    fi
  done
fi

# Вход нужен, если данные входа не вписаны в .env вручную. Если вход уже выполнен, login ничего не спрашивает.
if [ -z "$(env_get PORTALS_AUTH_DATA)" ] || [ -z "$(env_get TONNEL_AUTH_DATA)" ]; then
  step "Вход в Telegram-аккаунт"
  until run_bot login; do
    echo
    echo "Если в ошибке выше есть ApiIdInvalid — неверные API_ID или API_HASH."
    read -r -p "Enter — попробовать ещё раз, 2 — ввести API_ID и API_HASH заново, q — выйти: " answer || answer=q
    case $answer in
      2)
        reask API_ID "API_ID с my.telegram.org (число)" "$API_ID_RE"
        reask API_HASH "API_HASH с my.telegram.org (32 символа)" "$API_HASH_RE"
        ;;
      q* | Q* | й* | Й*) die "Вход не выполнен — когда будете готовы, запустите install.sh ещё раз" ;;
    esac
  done
fi

# ------------------------------------------------------------------ проверка и запуск

if [ "$MODE" = diag ]; then
  step "Диагностика площадок"
  run_bot diag || warn "Диагностика завершилась с ошибкой, подробности выше"
  if [ "$restart_on_abort" = 1 ]; then  # бот работал до диагностики — возвращаем как было
    restart_on_abort=0
    systemctl start "$SERVICE" || true
  fi
  echo
  echo "Пришлите этот отчёт целиком. Он сохранён в $data_dir/diag.txt (показать снова: ${SUDO}cat $data_dir/diag.txt)"
  exit 0
fi

step "Проверка: Telegram-бот и площадки"
if run_bot check; then
  ok "Проверка пройдена"
else
  warn "Проверка нашла ошибки, подробности выше"
  read -r -p "Всё равно запустить бота? [y/N] " answer || answer=n
  case $answer in
    y* | Y* | д* | Д*) ;;
    *)
      restart_on_abort=0  # вы сами решили не запускать
      die "Бот не запущен. Исправьте настройки (${SUDO}nano $ENV_FILE) и запустите install.sh ещё раз"
      ;;
  esac
fi

step "Автозапуск"
restart_on_abort=0
if ! has_systemd; then
  warn "systemd не найден. Запустите вручную: cd $APP_DIR && ${SUDO}runuser -u $APP_USER -- .venv/bin/python -m giftbot run"
  exit 0
fi
sed -e "s#/opt/gift-arbitrage-bot#$APP_DIR#g" -e "s#^User=.*#User=$APP_USER#" -e "s#^Group=.*#Group=$APP_USER#" \
  "$APP_DIR/giftbot.service" > "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl restart "$SERVICE"
sleep 5
if ! systemctl is-active --quiet "$SERVICE"; then
  warn "Бот не запустился. Последние строки лога:"
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  exit 1
fi
ok "Бот работает и сам запустится после перезагрузки сервера"

cat <<EOF

Полезные команды:
  ${SUDO}journalctl -u $SERVICE -f              лог в реальном времени (выход: Ctrl+C)
  ${SUDO}systemctl restart $SERVICE        перезапуск (нужен после правки настроек)
  ${SUDO}systemctl stop $SERVICE           остановка
  ${SUDO}nano $ENV_FILE    настройки
Обновление бота:
  cd $SRC_DIR && git pull && ${SUDO}bash install.sh
EOF
