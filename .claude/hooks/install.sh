#!/usr/bin/env bash
# 日本語チェックフックをユーザー設定 (~/.claude) に入れて、全プロジェクトで自動で動かす。
# 何度実行しても同じ状態になる。既存の ~/.claude/settings.json は壊さずマージする。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$DEST/hooks"

cp "$SRC/ja-style-check.py" "$DEST/hooks/ja-style-check.py"
if [ -f "$SRC/../agents/ja-proofreader.md" ]; then
  mkdir -p "$DEST/agents"
  cp "$SRC/../agents/ja-proofreader.md" "$DEST/agents/ja-proofreader.md"
fi
cp "$SRC/ja-ng-phrases.json" "$DEST/hooks/ja-ng-phrases.json"
chmod +x "$DEST/hooks/ja-style-check.py"

python3 - "$DEST" <<'PY'
import json, os, sys

dest = sys.argv[1]
path = os.path.join(dest, "settings.json")
cmd = 'python3 "%s/hooks/ja-style-check.py"' % dest

settings = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        try:
            settings = json.load(fh)
        except json.JSONDecodeError:
            sys.exit("既存の %s が壊れています。直してから再実行してください。" % path)
    with open(path + ".bak", "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)

hooks = settings.setdefault("hooks", {})
stop = hooks.setdefault("Stop", [])

for group in stop:
    for h in group.get("hooks", []):
        if "ja-style-check.py" in h.get("command", ""):
            h["command"] = cmd
            h["timeout"] = 90
            break
    else:
        continue
    break
else:
    stop.append({"matcher": "",
                 "hooks": [{"type": "command", "command": cmd, "timeout": 90}]})

with open(path, "w", encoding="utf-8") as fh:
    json.dump(settings, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("registered Stop hook in " + path)
PY

echo
echo "設置しました。新しいセッションから全プロジェクトで自動で動きます。"
echo "一時的に止める場合: export JA_CHECK_DISABLE=1"
if [ -f "$DEST/agents/ja-proofreader.md" ]; then
  echo "校正エージェント: $DEST/agents/ja-proofreader.md"
  echo "長文パイプラインを全プロジェクトで使うなら、CLAUDE.md の内容を $DEST/CLAUDE.md に足してください。"
fi
