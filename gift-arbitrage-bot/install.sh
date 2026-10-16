#!/usr/bin/env bash
# Установка и обновление бота на VPS с Ubuntu 22.04+ или Debian 12+:
#
#   sudo bash install.sh
#
# Первый запуск ставит Python, создаёт пользователя giftbot, копирует бота в /opt/gift-arbitrage-bot,
# спрашивает токены, узнаёт ваш chat_id, входит в Telegram-аккаунт, проверяет площадки и включает
# автозапуск. Повторный запуск обновляет код и зависимости, а настройки (.env) и папку data/ не трогает.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/gift-arbitrage-bot}"
APP_USER="${APP_USER:-giftbot}"
SERVICE="${SERVICE:-giftbot}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"
PY="$APP_DIR/.venv/bin/python"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
has_systemd() { [ -d /run/systemd/system ]; }

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

[ "$(id -u)" -eq 0 ] || die "Запустите от root: sudo bash install.sh"
command -v apt-get >/dev/null || die "Скрипт рассчитан на Ubuntu или Debian (нужен apt-get)"
[ -f "$SRC_DIR/giftbot/__main__.py" ] || die "Рядом со скриптом нет бота: запускайте install.sh из папки gift-arbitrage-bot"

# ------------------------------------------------------------------ система

step "Системные пакеты"
if ! command -v python3 >/dev/null || ! python3 -c 'import ensurepip' 2>/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv >/dev/null
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
if has_systemd && systemctl is-active --quiet "$SERVICE"; then
  systemctl stop "$SERVICE"
  ok "Работающий бот остановлен на время обновления"
  # если обновление прервётся, бот останется выключенным — подскажем, как его вернуть
  trap '[ $? -eq 0 ] || warn "Бот сейчас не работает. Запустить снова: systemctl start $SERVICE"' EXIT
fi
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  rm -rf "$APP_DIR/giftbot" "$APP_DIR/tests"
  tar -C "$SRC_DIR" --exclude=./.env --exclude=./data --exclude=./.venv --exclude=./.git --exclude=__pycache__ -cf - . \
    | tar -C "$APP_DIR" --no-same-owner --no-same-permissions -xf -
fi
ok "Код на месте"

step "Зависимости Python (1–3 минуты)"
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  rm -rf "$APP_DIR/.venv"
  python3 -m venv "$APP_DIR/.venv"
fi
"$PY" -m pip install -q --disable-pip-version-check --upgrade pip
"$PY" -m pip install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
ok "Зависимости установлены"

# ------------------------------------------------------------------ настройки

step "Настройки"
if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"
  ok "Создан $ENV_FILE"
fi
data_dir=$(env_get DATA_DIR)
data_dir=${data_dir:-data}
[[ $data_dir = /* ]] || data_dir="$APP_DIR/$data_dir"
mkdir -p "$data_dir"
chown -R "$APP_USER:$APP_USER" "$data_dir"
chmod 700 "$data_dir"

ask BOT_TOKEN "Токен бота от @BotFather (вида 123456:AA...)" '^[0-9]{5,}:[A-Za-z0-9_-]{30,}$'
ask API_ID "API_ID с my.telegram.org (число)" '^[0-9]{3,}$'
ask API_HASH "API_HASH с my.telegram.org (32 символа)" '^[0-9a-fA-F]{32}$'
# .env читает только бот, менять может только root: в нём ключи от вашего аккаунта
chown "root:$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

if [ -z "$(env_get ADMIN_CHAT_ID)" ]; then
  step "Узнаю ваш chat_id"
  echo "Откройте своего бота в Telegram и отправьте ему /start."
  out=$(mktemp)
  run_bot chatid | tee "$out" || true
  chat_id=$(sed -n 's/.*chat_id: \(-\{0,1\}[0-9]\{1,\}\).*/\1/p' "$out" | tail -n 1)
  rm -f "$out"
  if [ -n "$chat_id" ]; then
    env_set ADMIN_CHAT_ID "$chat_id"
    ok "ADMIN_CHAT_ID=$chat_id"
  else
    warn "Не получилось узнать chat_id автоматически"
    ask ADMIN_CHAT_ID "Введите chat_id вручную (число)" '^-?[0-9]+$'
  fi
fi

# Вход нужен, если данные входа не вписаны в .env вручную. Если вход уже выполнен, login ничего не спрашивает.
if [ -z "$(env_get PORTALS_AUTH_DATA)" ] || [ -z "$(env_get TONNEL_AUTH_DATA)" ]; then
  step "Вход в Telegram-аккаунт"
  first_login=0
  [ -f "$data_dir/telegram.session" ] || first_login=1
  run_bot login || die "Вход не выполнен — запустите install.sh ещё раз"
  if [ "$first_login" = 1 ]; then
    echo
    echo "Откройте с этого аккаунта мини-приложения @portals и @Tonnel_Network_bot хотя бы по разу."
    read -r -p "Когда откроете, нажмите Enter… " _ || true
  fi
fi

# ------------------------------------------------------------------ проверка и запуск

step "Проверка: Telegram-бот и площадки"
if run_bot check; then
  ok "Проверка пройдена"
else
  warn "Проверка нашла ошибки, подробности выше"
  read -r -p "Всё равно запустить бота? [y/N] " answer || answer=n
  case $answer in
    y* | Y* | д* | Д*) ;;
    *) die "Исправьте настройки (sudo nano $ENV_FILE) и запустите install.sh ещё раз" ;;
  esac
fi

step "Автозапуск"
if ! has_systemd; then
  warn "systemd не найден. Запустите вручную: cd $APP_DIR && sudo -u $APP_USER .venv/bin/python -m giftbot run"
  exit 0
fi
sed -e "s#/opt/gift-arbitrage-bot#$APP_DIR#g" -e "s#^User=.*#User=$APP_USER#" -e "s#^Group=.*#Group=$APP_USER#" \
  "$APP_DIR/giftbot.service" > "/etc/systemd/system/$SERVICE.service"
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
  journalctl -u $SERVICE -f              лог в реальном времени (выход: Ctrl+C)
  sudo systemctl restart $SERVICE        перезапуск (нужен после правки настроек)
  sudo systemctl stop $SERVICE           остановка
  sudo nano $ENV_FILE    настройки
Обновление бота:
  cd $SRC_DIR && git pull && sudo bash install.sh
EOF
