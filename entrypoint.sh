#!/bin/bash
# Держит бота запущенным и обновляет его код из git. Лежит в образе, код бота — в томе /app.
APP_DIR="${APP_DIR:-/app}"; DATA_DIR="${DATA_DIR:-/data}"; BRANCH="${BRANCH:-main}"; PY="${PYTHON:-python}"
: "${REPO_URL:?REPO_URL не задан}"
mkdir -p "$DATA_DIR" "$APP_DIR"; cd "$APP_DIR" || exit 1
log() { echo "[entrypoint] $*"; }
stopping=0; child=
trap 'stopping=1; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null' TERM INT

deps() { [ -f requirements.txt ] || return 0; "$PY" -m pip install -q --no-cache-dir -r requirements.txt; }

update() {            # 0 — код обновился
  if [ ! -d .git ]; then
    find . -mindepth 1 -delete 2>/dev/null
    git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" . || { log "clone не удался"; return 1; }
    log "склонировал $(git rev-parse --short HEAD)"; return 1
  fi
  git fetch -q --depth 1 origin "$BRANCH" || { log "fetch не удался, работаю на текущей версии"; return 1; }
  local new cur blocked
  new=$(git rev-parse FETCH_HEAD); cur=$(git rev-parse HEAD); blocked=$(cat "$DATA_DIR/blocked_commit" 2>/dev/null)
  [ "$new" = "$cur" ] && return 1
  [ "$new" = "$blocked" ] && { log "коммит ${new:0:7} уже не запускался, пропускаю"; return 1; }
  echo "$cur" > "$DATA_DIR/prev_commit"
  git reset -q --hard "$new" && log "обновился ${cur:0:7} -> ${new:0:7}"
}

rollback() {          # $1 — причина
  local bad prev; bad=$(git rev-parse HEAD); prev=$(cat "$DATA_DIR/prev_commit")
  echo "$bad" > "$DATA_DIR/blocked_commit"; git reset -q --hard "$prev"
  echo "⚠️ Обновление ${bad:0:7} не запустилось ($1). Вернул прежнюю версию ${prev:0:7}, этот коммит больше пробовать не буду." > "$DATA_DIR/notice.txt"
  log "откат: $1"
}

while [ $stopping -eq 0 ]; do
  UPDATED=0; update && UPDATED=1
  if ! deps; then
    log "pip install не удался"
    if [ $UPDATED -eq 1 ]; then rollback "зависимости"; continue; fi
  fi
  start=$(date +%s)
  "$PY" bot.py & child=$!
  wait $child; code=$?
  if [ $stopping -eq 1 ]; then wait $child 2>/dev/null; break; fi
  child=
  dur=$(( $(date +%s) - start ))
  [ $code -eq 42 ] && { log "перезапуск для обновления"; continue; }
  if [ $UPDATED -eq 1 ] && [ $code -ne 0 ] && [ $dur -lt 40 ]; then rollback "упал через ${dur} с, код $code"; continue; fi
  log "бот завершился с кодом $code, перезапуск через 5 с"; sleep 5
done
log "остановлен"
