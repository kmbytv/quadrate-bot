#!/usr/bin/env bash
# Переводит РАБОЧЕГО бота из ~/reportbot с OpenRouter на платформу OpenAI
# и запускает его как systemd-сервис quadrate-bot. Код бота не заменяется —
# меняются только адрес API, ключ и параметры запроса.
#
#   curl -fsSL https://raw.githubusercontent.com/kmbytv/quadrate-bot/claude/bot-gs-error-viclrs/scripts/switch-to-openai.sh | bash
#
# Перед правкой делает полный бэкап папки. Если что-то не сходится — ничего
# не трогает и останавливается.

set -euo pipefail

BOT_DIR="${BOT_DIR:-/root/reportbot}"
SERVICE="quadrate-bot"
MODEL="${MODEL:-gpt-6-luna}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запусти от root."
[ -f "$BOT_DIR/bot.py" ] || die "Не нашёл $BOT_DIR/bot.py"
[ -f "$BOT_DIR/.env" ]   || die "Не нашёл $BOT_DIR/.env"

ENV_FILE="$BOT_DIR/.env"
get_env() { grep -E "^$1=" "$2" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }
set_env() {
    local key="$1" val="$2" tmp="$ENV_FILE.tmp"
    grep -vE "^$key=" "$ENV_FILE" > "$tmp" || true
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}

# ── 1. Ключ OpenAI ────────────────────────────────────────────────────────────
key="$(get_env OPENAI_API_KEY "$ENV_FILE")"
[ -n "$key" ] && [[ "$key" != your_* ]] || key="$(get_env OPENAI_API_KEY /opt/quadrate-bot/.env)"
if [ -z "$key" ] || [[ "$key" == your_* ]]; then
    { : < /dev/tty; } 2>/dev/null || die "Нет OPENAI_API_KEY. Впиши его в $ENV_FILE и запусти снова."
    read -r -p "Ключ OpenAI (sk-...): " key < /dev/tty
fi
[[ "$key" == sk-* ]] || die "Ключ OpenAI должен начинаться с sk-"

# ── 2. Бэкап ──────────────────────────────────────────────────────────────────
backup="${BOT_DIR}.backup-$(date +%Y%m%d-%H%M%S)"
say "Бэкап: $backup"
cp -a "$BOT_DIR" "$backup"

# ── 3. Правка кода ────────────────────────────────────────────────────────────
say "Переключаю код на OpenAI"
rc=0
python3 - "$BOT_DIR" "$MODEL" <<'PY' || rc=$?
import pathlib, re, sys

root = pathlib.Path(sys.argv[1])
files = [p for p in root.glob("*.py")]
changed = {}

for p in files:
    s = orig = p.read_text(encoding="utf-8")
    if "openrouter" not in s.lower():
        continue
    # Адрес API: и полный URL, и базовый.
    s = re.sub(r"https://openrouter\.ai/api/v1", "https://api.openai.com/v1", s)
    # Переменная с ключом.
    s = s.replace("OPENROUTER_API_KEY", "OPENAI_API_KEY")
    # Reasoning-модели OpenAI не принимают temperature и max_tokens.
    s = re.sub(r'^[ \t]*["\']temperature["\']\s*:\s*[^,\n]+,[ \t]*\n', "", s, flags=re.M)
    s = re.sub(r'(["\'])max_tokens\1(\s*:)', r'\1max_completion_tokens\1\2', s)
    # Именованные аргументы правим ТОЛЬКО внутри вызова SDK (...completions.create(...)),
    # чтобы не задеть собственные функции бота с параметром max_tokens.
    def fix_call(m):
        c = re.sub(r'\btemperature\s*=\s*[^,)\n]+,?\s*', "", m.group(0))
        return re.sub(r'\bmax_tokens(\s*=)', r'max_completion_tokens\1', c)
    s = re.sub(r'completions\.create\((?:[^()]|\([^()]*\))*\)', fix_call, s, flags=re.S)
    # Глубина рассуждений: low — быстрее и дешевле.
    if "reasoning_effort" not in s and "import os" in s:
        s = re.sub(r'^([ \t]*)(["\'])max_completion_tokens\2(\s*:\s*[^,\n]+,)',
                   r'\g<0>\n\1"reasoning_effort": os.getenv("LLM_REASONING_EFFORT", "low"),',
                   s, flags=re.M)
    # Модели в формате OpenRouter (vendor/model) OpenAI не знает.
    s = re.sub(r'(["\'])(?:anthropic|openai|google|deepseek|meta-llama|mistralai|x-ai|qwen)/[\w.:-]+\1',
               lambda m: m.group(1) + sys.argv[2] + m.group(1), s)
    # Заголовки OpenRouter для OpenAI не нужны.
    s = re.sub(r'^[ \t]*["\'](HTTP-Referer|X-Title)["\']\s*:\s*[^,\n]+,[ \t]*\n', "", s, flags=re.M)
    if s != orig:
        p.write_text(s, encoding="utf-8")
        changed[p.name] = True

left = [p.name for p in files if "openrouter" in p.read_text(encoding="utf-8").lower()]
if not changed:
    if any("api.openai.com" in p.read_text(encoding="utf-8") for p in files):
        print("Код уже переключён на OpenAI — пропускаю.")
        sys.exit(0)
    print("НЕ НАШЁЛ вызовов OpenRouter в коде — ничего не менял.")
    sys.exit(3)
print("Изменены файлы:", ", ".join(sorted(changed)))
if left:
    print("Упоминания openrouter остались в:", ", ".join(left), "(обычно это комментарии)")
PY
[ $rc -eq 0 ] || die "Код не распознан — пришли мне вывод: grep -n -i openrouter $BOT_DIR/*.py"

# ── 4. .env ───────────────────────────────────────────────────────────────────
say "Обновляю .env"
set_env OPENAI_API_KEY "$key"
set_env LLM_MODEL "$MODEL"
set_env LLM_REASONING_EFFORT low
set_env PROXY_URL ""
# Любая переменная с моделью в формате OpenRouter (vendor/model) → модель OpenAI.
for k in $(grep -oE '^[A-Z_]*MODEL[A-Z_]*=' "$ENV_FILE" | tr -d = || true); do
    v="$(get_env "$k" "$ENV_FILE")"
    [[ "$v" == */* ]] && set_env "$k" "$MODEL"
done
grep -vE '^OPENROUTER_API_KEY=' "$ENV_FILE" > "$ENV_FILE.tmp" || true
mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"

# ── 5. Python ─────────────────────────────────────────────────────────────────
PY=""
for c in "$BOT_DIR/venv/bin/python" "$BOT_DIR/.venv/bin/python" /opt/quadrate-bot/venv/bin/python; do
    [ -x "$c" ] && PY="$c" && break
done
[ -n "$PY" ] || PY="$(command -v python3)"
if ! "$PY" -c "import telegram, httpx, dotenv" 2>/dev/null; then
    say "Ставлю зависимости в $BOT_DIR/venv"
    python3 -m venv "$BOT_DIR/venv"
    PY="$BOT_DIR/venv/bin/python"
    "$PY" -m pip install --quiet --upgrade pip
    if [ -f "$BOT_DIR/requirements.txt" ]; then
        "$PY" -m pip install --quiet -r "$BOT_DIR/requirements.txt"
    else
        "$PY" -m pip install --quiet python-telegram-bot httpx python-dotenv
    fi
fi
echo "Python: $PY"

restore() {
    warn "Откатываю код из бэкапа"
    rm -rf "$BOT_DIR"; cp -a "$backup" "$BOT_DIR"
}
if ! (cd "$BOT_DIR" && "$PY" -m py_compile ./*.py); then
    restore; die "После правки код не компилируется — откатил. Пришли мне вывод выше."
fi

# ── 6. Останавливаем всё старое ───────────────────────────────────────────────
say "Останавливаю прежние запуски бота"
systemctl stop "$SERVICE" 2>/dev/null || true
for pid in $(pgrep -f python || true); do
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    if [ "$cwd" = "$BOT_DIR" ] || [ "$cwd" = /opt/quadrate-bot ]; then kill "$pid" 2>/dev/null || true; fi
done
sleep 2

# ── 7. Сервис на рабочем коде ─────────────────────────────────────────────────
say "Запускаю сервис $SERVICE из $BOT_DIR"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Quadrate daily reports Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$BOT_DIR
ExecStart=$PY $BOT_DIR/bot.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl restart "$SERVICE"

sleep 15
if systemctl is-active --quiet "$SERVICE"; then
    say "Готово ✅ Бот работает на OpenAI ($MODEL)"
else
    warn "Сервис не поднялся — логи ниже"
fi
journalctl -u "$SERVICE" -n 12 --no-pager | grep -v -E 'api\.telegram\.org/bot' || true

cat <<EOF

Бэкап старого кода: $backup
Откат одной командой:
  systemctl stop $SERVICE && rm -rf $BOT_DIR && cp -a $backup $BOT_DIR && systemctl start $SERVICE

Проверь: /report и короткое гс боту — строка должна появиться в «Сводке дня».
EOF
