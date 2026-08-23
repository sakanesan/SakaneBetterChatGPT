#!/usr/bin/env python3
"""Stop hook: 日本語の返答から AI 特有の言い回しを検出し、見つかったらターンを差し戻す。

判定は二段構え。

  1. 正規表現 (ja-ng-phrases.json) — 前口上・決め文・ぼかしなど定型のもの。
     ほぼ全ての違反はここで捕まるので LLM を呼ばずに済む。
  2. claude -p の小型モデル — 段落末の決め文、同じ文末の連続、直前の言い換えなど
     文脈を読まないと判定できないもの。テキストが一定長を超えたときだけ呼ぶ。

違反があれば {"decision": "block", "reason": ...} を stdout に出す。reason には
実際に出力した表現をそのまま引用する。ルールを再掲するだけでは直らないため。

無限ループを避けるため、同一ユーザーターンでの差し戻しは MAX_BLOCKS 回まで。
何が起きても本体セッションを壊さないよう、例外は全て握り潰して allow する。

環境変数:
  JA_CHECK_DISABLE=1   フックを完全に無効化する（子プロセスにも渡してある）
  JA_CHECK_MODE        regex | hybrid(既定) | llm
  JA_CHECK_MODEL       判定に使うモデル（既定: claude-haiku-4-5-20251001）
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

MAX_BLOCKS = 2
LLM_MIN_CHARS = 250
MIN_CHARS = 30
LLM_TIMEOUT = 60
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

HERE = os.path.dirname(os.path.abspath(__file__))
KANA = re.compile(r"[ぁ-んァ-ヶ]")


def allow():
    sys.exit(0)


# ---------------------------------------------------------------- transcript

def load_turn(transcript_path):
    """直近のユーザー発話以降に本文として出た assistant テキストを返す。"""
    entries = []
    with open(transcript_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    last_user = -1
    for i, e in enumerate(entries):
        if e.get("type") != "user":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str):
            last_user = i
        elif isinstance(content, list):
            # tool_result だけの user エントリはターンの区切りではない
            if any(c.get("type") == "text" for c in content if isinstance(c, dict)):
                last_user = i

    marker = entries[last_user].get("uuid", str(last_user)) if last_user >= 0 else "head"

    chunks = []
    for e in entries[last_user + 1:]:
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    chunks.append(c.get("text", ""))
    return marker, "\n".join(chunks)


def strip_noise(text):
    """コードやパス、引用を落として、地の文だけ残す。"""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s*>.*$", " ", text, flags=re.M)
    text = re.sub(r"[\w./-]*/[\w./-]+", " ", text)
    # 鉤括弧・引用符の中は「引用した他人の文」なので地の文として数えない。
    # NG 表現を例として示すときに自分が書いたと誤判定されるのを防ぐ。
    text = re.sub(r"「[^「」]{0,120}」", " ", text)
    text = re.sub(r"『[^『』]{0,120}』", " ", text)
    text = re.sub(r"\"[^\"\n]{0,120}\"", " ", text)
    return text


# -------------------------------------------------------------------- rules

def load_rules():
    paths = [os.path.join(HERE, "ja-ng-phrases.json"),
             os.path.expanduser("~/.claude/ja-ng-phrases.local.json")]
    merged = {}
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for rule in json.load(fh).get("rules", []):
                    merged[rule["pattern"]] = rule
        except Exception:
            continue
    return [r for r in merged.values() if r.get("severity") != "off"]


def scan(text, rules):
    hard, soft = [], []
    for rule in rules:
        try:
            m = re.search(rule["pattern"], text)
        except re.error:
            continue
        if not m:
            continue
        hit = {"quote": sentence_at(text, m.start(), m.end()),
               "matched": m.group(0),
               "label": rule["label"], "fix": rule["fix"]}
        (hard if rule.get("severity") == "hard" else soft).append(hit)
    return hard, soft


def sentence_at(text, start, end):
    """該当箇所を含む一文を返す。長すぎる場合は前後を詰める。"""
    head = max(text.rfind("\n", 0, start), text.rfind("。", 0, start) + 1,
               text.rfind("、", 0, start) + 1 if start - text.rfind("。", 0, start) > 60 else -1)
    head = max(head, 0)
    tail = text.find("。", end)
    tail = len(text) if tail < 0 else tail + 1
    nl = text.find("\n", end)
    if 0 <= nl < tail:
        tail = nl
    s = text[head:tail].strip()
    if len(s) > 70:
        s = text[max(head, start - 20):min(tail, end + 20)].strip()
    return s.replace("\n", " ")


# ---------------------------------------------------------------------- LLM

PROMPT = """あなたは日本語の校閲者です。次の文章に、AIが書いたと分かる不自然な表現が
含まれているかだけを判定してください。

検出対象:
- 前口上（事実を伝えず、これから説明する内容や読み方を先に案内する文）
- 段落末の決め文（事実を比喩・対句・抽象的な要約で締め直す文）
- 直前の内容を別の言葉で言い直しただけの文
- 言い切れる内容を不要にぼかす文末
- 近接する文で同じ文末が繰り返される箇所
- 英語の語順や名詞句をそのまま移したような文

検出しないもの:
- コード、コマンド、ファイルパス、識別子、技術用語
- 見出し、箇条書きの短い項目
- 事実や手順をそのまま述べている文

明らかな違反だけを最大3件、次のJSONだけで出力してください。違反がなければ
{"violations": []} と出力してください。前後に説明を書かないでください。

{"violations": [{"quote": "問題のある箇所をそのまま引用", "reason": "何が問題か20字以内"}]}

--- 対象の文章 ---
"""


def ask_llm(text):
    binary = shutil.which("claude")
    if not binary:
        return []
    env = dict(os.environ)
    env["JA_CHECK_DISABLE"] = "1"  # 子プロセスで同じフックが再帰しないように
    model = os.environ.get("JA_CHECK_MODEL", DEFAULT_MODEL)
    base = [binary, "-p", "--model", model]
    # --setting-sources '' で設定を一切読ませない（フックが再帰しない、起動も速い）
    isolated = base + ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--setting-sources", ""]

    def run(argv):
        return subprocess.run(argv, input=PROMPT + text[:6000],
                              capture_output=True, text=True, timeout=LLM_TIMEOUT,
                              cwd=tempfile.gettempdir(), env=env)

    try:
        proc = run(isolated)
        if proc.returncode != 0:  # 古い CLI ではフラグが無い
            proc = run(base)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    m = re.search(r"\{.*\}", proc.stdout, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for v in data.get("violations", [])[:3]:
        if isinstance(v, dict) and v.get("quote"):
            out.append({"quote": str(v["quote"])[:120],
                        "label": "文脈判定",
                        "fix": str(v.get("reason", "書き直す"))[:60]})
    return out


# -------------------------------------------------------------------- state

def state_path(session_id):
    d = os.path.join(tempfile.gettempdir(), "ja-style-check")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "nosession")
    return os.path.join(d, safe + ".json")


def block_count(session_id, marker):
    try:
        with open(state_path(session_id), encoding="utf-8") as fh:
            st = json.load(fh)
        return st.get("count", 0) if st.get("marker") == marker else 0
    except Exception:
        return 0


def bump(session_id, marker, count):
    try:
        with open(state_path(session_id), "w", encoding="utf-8") as fh:
            json.dump({"marker": marker, "count": count}, fh)
    except Exception:
        pass


# --------------------------------------------------------------------- main

def main():
    if os.environ.get("JA_CHECK_DISABLE") == "1":
        allow()

    payload = json.load(sys.stdin)
    transcript = payload.get("transcript_path")
    session_id = payload.get("session_id", "")
    if not transcript or not os.path.exists(transcript):
        allow()

    marker, raw = load_turn(transcript)
    if not raw.strip():
        allow()  # ツールだけのターン

    text = strip_noise(raw)
    if len(text.strip()) < MIN_CHARS or not KANA.search(text):
        allow()  # 日本語の地の文がない

    count = block_count(session_id, marker)
    if count >= MAX_BLOCKS:
        allow()

    rules = load_rules()
    hard, soft = scan(text, rules)

    mode = os.environ.get("JA_CHECK_MODE", "hybrid")
    hits = list(hard) + (soft if len(soft) >= 2 else [])
    if mode == "llm" or (mode == "hybrid" and len(text) >= LLM_MIN_CHARS):
        hits += ask_llm(text)

    if not hits:
        allow()

    # 同じ文に複数の指摘が付いたら 1 件にまとめる。同じ引用が並ぶと直しにくい。
    grouped, order = {}, []
    for h in hits[:8]:
        q = h["quote"]
        if q not in grouped:
            grouped[q] = []
            order.append(q)
        note = "{}。{}".format(h["label"], h["fix"])
        if note not in grouped[q]:
            grouped[q].append(note)

    lines = []
    for q in order[:5]:
        lines.append('- 「{}」\n  → {}'.format(q, " / ".join(grouped[q][:3])))

    reason = (
        "[日本語チェック] 今の返答に、AIが書いたと分かる表現が残っています。\n\n"
        + "\n".join(lines)
        + "\n\n該当箇所だけを直した返答の全文を、もう一度出力してください。\n"
        "・説明、謝罪、変更点の一覧は書かない\n"
        "・同じ内容を別の言い回しで繰り返さない\n"
        "・削除して意味が通る文は消す\n"
        "・指摘のなかった箇所は変えない"
    )

    bump(session_id, marker, count + 1)
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        allow()
