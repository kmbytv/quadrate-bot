#!/usr/bin/env bash
# Установка и обновление бота Quadrate одной командой (Ubuntu/Debian, от root):
#
#   curl -fsSL https://raw.githubusercontent.com/kmbytv/quadrate-bot/main/deploy.sh | bash
#
# Что делает:
#   1. Ставит git и python3-venv, если их нет.
#   2. Скачивает (или обновляет) код в /opt/quadrate-bot.
#   3. Ставит зависимости в отдельный venv.
#   4. Находит .env старого бота и переносит его; спрашивает недостающие ключи.
#   5. Гасит старый запущенный процесс бота (иначе два бота дерутся за один токен).
#   6. Запускает бота как systemd-сервис: сам поднимается после падения и перезагрузки.
#
# Повторный запуск = обновление до свежей версии с GitHub и перезапуск.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/kmbytv/quadrate-bot.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/quadrate-bot}"
SERVICE="quadrate-bot"
DEFAULT_MODEL="gpt-6-luna"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запусти от root (или через sudo)."

# ── 1. Системные пакеты ───────────────────────────────────────────────────────
say "Проверяю системные пакеты"
need=()
command -v git >/dev/null || need+=(git)
python3 -c "import venv, ensurepip" 2>/dev/null || need+=(python3-venv)
if [ ${#need[@]} -gt 0 ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" python3-pip
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Нужен Python 3.10+, а стоит $(python3 --version)."

# ── 2. Ищем старого бота (до того, как что-то трогать) ───────────────────────
# Свой процесс узнаём по содержимому: в bot.py есть слово Quadrate.
old_pids=()
old_dir=""
for pid in $(pgrep -f python || true); do
    [ "$pid" = "$$" ] && continue
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    [ -n "$cwd" ] || continue
    if grep -qs "Quadrate" "$cwd"/*.py; then
        if grep -qs "${SERVICE}.service" "/proc/$pid/cgroup"; then
            continue  # это уже наш сервис — его перезапустим штатно
        fi
        old_pids+=("$pid")
        [ -z "$old_dir" ] && old_dir="$cwd"
    fi
done

# ── 3. Код ────────────────────────────────────────────────────────────────────
say "Скачиваю код ($BRANCH) в $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    git -C "$APP_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
    git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
else
    if [ -e "$APP_DIR" ] && [ "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
        backup="${APP_DIR}.backup.$(date +%Y%m%d-%H%M%S)"
        warn "$APP_DIR уже есть и это не git — переношу в $backup"
        mv "$APP_DIR" "$backup"
        if [ -z "$old_dir" ] || [ "$old_dir" = "$APP_DIR" ]; then old_dir="$backup"; fi
    fi
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
echo "Версия: $(git -C "$APP_DIR" log -1 --format='%h %s')"

# ── 4. Зависимости ────────────────────────────────────────────────────────────
say "Ставлю зависимости"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ── 5. .env ───────────────────────────────────────────────────────────────────
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    src=""
    if [ -n "$old_dir" ] && [ -f "$old_dir/.env" ]; then
        src="$old_dir/.env"
    else
        # Ищем .env старого бота в типичных местах.
        while IFS= read -r f; do
            if grep -qs '^BOT_TOKEN=' "$f" && grep -qsE 'SHEETBEST_URL|DEEPGRAM_API_KEY' "$f"; then
                src="$f"; break
            fi
        done < <(find /root /home /opt /srv -maxdepth 4 -name .env -not -path "$APP_DIR/*" 2>/dev/null)
    fi
    if [ -n "$src" ]; then
        say "Нашёл настройки старого бота: $src — копирую"
        cp "$src" "$ENV_FILE"
    else
        warn "Старый .env не найден — создаю новый, спрошу ключи"
        cp "$APP_DIR/.env.example" "$ENV_FILE"
    fi
fi
chmod 600 "$ENV_FILE"

get_env() { grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true; }
set_env() {
    local key="$1" val="$2" tmp="$ENV_FILE.tmp"
    grep -vE "^$key=" "$ENV_FILE" > "$tmp" || true
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}
ask_env() {  # спросить ключ, если он пустой или остался заглушкой из примера
    local key="$1" prompt="$2" cur val
    cur="$(get_env "$key")"
    if [ -z "$cur" ] || [[ "$cur" == your_* ]] || [[ "$cur" == *YOUR_* ]]; then
        { : < /dev/tty; } 2>/dev/null || die "Нет $key в $ENV_FILE, а спросить не у кого. Впиши руками и перезапусти."
        read -r -p "$prompt: " val < /dev/tty
        [ -n "$val" ] || die "$key обязателен."
        set_env "$key" "$val"
    fi
}

say "Проверяю ключи в .env"
ask_env BOT_TOKEN        "Токен Telegram-бота (BOT_TOKEN)"
ask_env DEEPGRAM_API_KEY "Ключ Deepgram (DEEPGRAM_API_KEY)"
ask_env SHEETBEST_URL    "Ссылка SheetBest (SHEETBEST_URL)"
ask_env OPENAI_API_KEY   "Ключ OpenAI с platform.openai.com (OPENAI_API_KEY, начинается с sk-)"

# Модель: старые ID в формате OpenRouter (vendor/model) OpenAI не поймёт.
model="$(get_env LLM_MODEL)"
if [ -z "$model" ] || [[ "$model" == */* ]]; then
    [ -n "$model" ] && echo "LLM_MODEL: $model → $DEFAULT_MODEL"
    set_env LLM_MODEL "$DEFAULT_MODEL"
fi
[ -n "$(get_env LLM_REASONING_EFFORT)" ] || set_env LLM_REASONING_EFFORT low

# ── 6. Гасим старого бота ─────────────────────────────────────────────────────
if [ ${#old_pids[@]} -gt 0 ]; then
    say "Останавливаю старого бота (PID ${old_pids[*]})"
    for pid in "${old_pids[@]}"; do
        # Если его держит другой systemd-сервис — отключаем сервис, иначе он воскреснет.
        unit="$(grep -oE '[^/]+\.service' "/proc/$pid/cgroup" 2>/dev/null | head -n1 || true)"
        if [ -n "$unit" ] && [ "$unit" != "${SERVICE}.service" ]; then
            echo "Отключаю старый сервис $unit"
            systemctl disable --now "$unit" || true
        fi
        kill "$pid" 2>/dev/null || true
    done
    sleep 3
    for pid in "${old_pids[@]}"; do kill -9 "$pid" 2>/dev/null || true; done
    command -v pm2 >/dev/null && pm2 jlist 2>/dev/null | grep -q bot \
        && warn "Есть процессы pm2 — если старый бот был там, сделай: pm2 delete all && pm2 save" || true
fi

# ── 7. systemd-сервис ─────────────────────────────────────────────────────────
say "Настраиваю сервис $SERVICE"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Quadrate daily reports Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl restart "$SERVICE"

sleep 5
if systemctl is-active --quiet "$SERVICE"; then
    say "Готово, бот запущен ✅"
else
    warn "Сервис не поднялся — смотри логи ниже"
fi
journalctl -u "$SERVICE" -n 15 --no-pager || true

cat <<EOF

Полезное:
  journalctl -u $SERVICE -f          живые логи (выход: Ctrl+C, бот не остановится)
  systemctl restart $SERVICE         перезапуск
  systemctl status $SERVICE          состояние
  nano $ENV_FILE                     настройки (после правки — restart)
  curl -fsSL https://raw.githubusercontent.com/kmbytv/quadrate-bot/main/deploy.sh | bash
                                     обновить до свежей версии
EOF
