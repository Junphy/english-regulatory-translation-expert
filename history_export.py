# history_export.py
"""运行记录与评估导出。

为什么不直接用 st.session_state.messages：回溯重译和编辑原文会截断并覆盖对话，
上一次的结果就丢了，没法做新旧 prompt 的对比；messages 里也只存了方案名，方案规则改过
但名字不变时，分不清是哪一版 prompt 跑的。所以这里另外维护一份“只追加、不覆盖”的运行记录，
每次调用（成功或失败）追加一条，并给方案规则存一份快照与短版本号。

记录只保存在当前浏览器会话里（刷新页面或会话过期即丢失），不写入 config.json，也不含 API 密钥。
"""
import hashlib
import json
import re
from collections import OrderedDict
from datetime import datetime, timezone

import streamlit as st

LOG_KEY = "run_log"


def scheme_version_id(scheme):
    """方案规则的短版本号：两份 prompt 的文字有任何改动，版本号就变。"""
    translator = (scheme or {}).get("translator", "") or ""
    reviewer = (scheme or {}).get("reviewer", "") or ""
    return hashlib.sha1((translator + "\0" + reviewer).encode("utf-8")).hexdigest()[:6]


def _runs():
    return st.session_state.setdefault(LOG_KEY, [])


def log_run(source_text, scheme_name, scheme, model, env_name, result, error, seconds, partial=None):
    """追加一条运行记录。result 是 (译文, Minimalist Correction, Polished Academic English, 方向标签) 或 None；
    error 是中文错误说明或 None；partial 是失败前已完成的内容（{"translation": ..., "review_raw": ...}）。"""
    runs = _runs()
    entry = {
        "seq": len(runs) + 1,
        "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(float(seconds), 1),
        "source": source_text,
        "scheme": scheme_name,
        "version": scheme_version_id(scheme),
        "rules": {"translator": (scheme or {}).get("translator", "") or "",
                  "reviewer": (scheme or {}).get("reviewer", "") or ""},
        "model": model,
        "interface": env_name,
        "direction": None, "translation": None, "minimal": None, "polished": None,
        "review_raw": None,
        "error": error or None,
    }
    if result is not None:
        entry["translation"], entry["minimal"], entry["polished"], entry["direction"] = result[0], result[1], result[2], result[3]
    elif partial:
        entry["translation"] = partial.get("translation")
        entry["review_raw"] = partial.get("review_raw")
    runs.append(entry)


def run_count():
    """(运行总数, 其中失败数, 不同原文条数)。"""
    runs = _runs()
    return len(runs), sum(1 for r in runs if r["error"]), len({_norm(r["source"]) for r in runs})


def clear_runs():
    st.session_state[LOG_KEY] = []


# ---------------------------------------------------------------- 导出
def _norm(text):
    return (text or "").replace("\r\n", "\n").strip()


def _fence(text):
    """用比内容里最长的反引号串更长的围栏包起来，译文里即使有 ``` 也不会破坏结构。"""
    longest = max([len(m) for m in re.findall(r"`+", text or "")] + [2])
    f = "`" * (longest + 1)
    return f"{f}text\n{text or ''}\n{f}"


def _letter(i):
    return chr(ord("a") + i) if i < 26 else str(i + 1)


def _hhmmss(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M:%S")
    except ValueError:
        return iso


def _select(runs, include_failed):
    return [r for r in runs if include_failed or not r["error"]]


def _group(runs):
    """按原文分组，编号按首次出现的顺序；组内按时间顺序，用 a、b、c 标运行。"""
    groups = OrderedDict()
    for r in runs:
        groups.setdefault(_norm(r["source"]), []).append(r)
    out = []
    for n, (_, items) in enumerate(groups.items(), start=1):
        for i, r in enumerate(items):
            r = dict(r)
            r["case_id"] = n
            r["run_id"] = f"{n}-{_letter(i)}"
            items[i] = r
        out.append((n, items))
    return out


def _version_text(r):
    return f"{r['scheme']}（{r['version']}）"


def build_markdown(runs, include_failed=False, include_rules=True):
    runs = _select(runs, include_failed)
    groups = _group(runs)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = ["# 翻译评估记录", "",
             f"- 导出时间：{now}（UTC）",
             f"- 共 {len(groups)} 条原文、{len(runs)} 次运行（{'已包含' if include_failed else '不含'}调用失败的运行）",
             "- 流程：Step 1 译者（中译英）→ Step 2 审校（一次输出 Minimalist Correction 与 Polished Academic English；最终译文是后者）",
             "- 版本号是方案两份 prompt（译者、审校）的短哈希：任何一份有改动，版本号就变",
             "- 发送给模型之前，原文里的“￼”已被删除；下面的“原文”是用户输入的原样",
             ""]
    if not groups:
        lines.append("（没有可导出的运行记录）")
        return "\n".join(lines) + "\n"

    lines += ["## 汇总", "", "| 编号 | 方向 | 运行次数 | 方案（版本）× 次数 |", "|---|---|---|---|"]
    for n, items in groups:
        dirs = list(OrderedDict.fromkeys(r["direction"] for r in items if r["direction"]))
        combos = OrderedDict()
        for r in items:
            combos[_version_text(r)] = combos.get(_version_text(r), 0) + 1
        lines.append(f"| {n} | {' / '.join(dirs) or '—'} | {len(items)} | " + "；".join(f"{k} × {v}" for k, v in combos.items()) + " |")
    lines.append("")

    for n, items in groups:
        dirs = list(OrderedDict.fromkeys(r["direction"] for r in items if r["direction"]))
        lines += ["---", "", f"## 编号 {n} · {' / '.join(dirs) or '方向未知'}", "", "**原文**", "", _fence(items[0]["source"]), ""]
        for r in items:
            head = f"### {r['run_id']} · 方案：{_version_text(r)} · 模型：{r['model']} · 方向：{r['direction'] or '中译英'} · {_hhmmss(r['time_utc'])} UTC · 用时 {r['seconds']} 秒"
            lines += [head, ""]
            if r["error"]:
                lines += ["**调用失败**", "", _fence(r["error"]), ""]
                if r.get("translation"):
                    lines += ["**失败前已完成的第 1 步 译文**", "", _fence(r["translation"]), ""]
            else:
                lines += ["**第 1 步 译文（译者）**", "", _fence(r["translation"]), "",
                          "**第 2 步 Minimalist Correction（审校，最小修改）**", "", _fence(r["minimal"]), "",
                          "**第 2 步 Polished Academic English（审校，最终译文）**", "", _fence(r["polished"]), ""]
            lines += ["**我认为的问题：**", "", "（留空，可在此补充）", ""]

    versions = OrderedDict()
    for _, items in groups:
        for r in items:
            versions.setdefault((r["scheme"], r["version"]), r["rules"])
    lines += ["---", "", "## 附：方案版本说明", ""]
    for (name, ver), rules in versions.items():
        title = f"{name}（{ver}）"
        if include_rules:
            lines += [f"### {title}", "", "**Step 1 译者 system prompt**", "", _fence(rules["translator"]), "",
                      "**Step 2 审校 system prompt**", "", _fence(rules["reviewer"]), ""]
        else:
            lines += [f"### {title}", "", "（未附规则全文）", ""]
    return "\n".join(lines).rstrip() + "\n"


def build_json(runs, include_failed=False, include_rules=True):
    runs = _select(runs, include_failed)
    out_runs = []
    for _, items in _group(runs):
        for r in items:
            r = dict(r)
            if not include_rules:
                r["rules"] = None
            out_runs.append(r)
    payload = {"exported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "note": "version 是方案两份 prompt（译者、审校）的短哈希；translation 是第 1 步译文，minimal 是 Minimalist Correction，polished 是 Polished Academic English（最终译文）；review_raw 只在审校输出无法拆分时保存模型原始输出；case_id 按原文首次出现顺序编号，run_id 为 编号-a/b/c。",
               "runs": out_runs}
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
