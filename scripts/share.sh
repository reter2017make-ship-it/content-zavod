#!/bin/bash
# Архив завода для передачи другому человеку: только код и инструкции из git (последний коммит).
# Ничего личного: ни .env, ни входа в Telegram (credentials/), ни базы, ни видео, ни моделей.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$HOME/Desktop/завод-для-передачи.zip}"
cd "$ROOT"
if [ -n "$(git status --porcelain -- . ':(exclude)data' 2>/dev/null)" ]; then
  echo "Внимание: есть незакоммиченные изменения — в архив попадёт последний коммит."
fi
rm -f "$OUT"
git archive --format=zip --prefix=kontent-zavod/ -o "$OUT" HEAD -- . ':(exclude)claude_desktop.docx'
# проверка: внутри не должно быть ничего личного
BAD=$(unzip -Z1 "$OUT" | grep -E '(^|/)\.env$|credentials/|\.session$|\.db$|^kontent-zavod/(data|database|models|logs|Готовые|Reels)/|browser_profiles/|\.onnx$|\.gguf$|\.mp4$' || true)
if [ -n "$BAD" ]; then
  echo "ОШИБКА: в архив попало личное, архив удалён:"; echo "$BAD"; rm -f "$OUT"; exit 1
fi
echo "Готово: $OUT ($(du -h "$OUT" | cut -f1), файлов: $(unzip -Z1 "$OUT" | wc -l | tr -d ' '))"
echo "Личных данных внутри нет: .env, вход в Telegram, база, видео и модели не включены."
