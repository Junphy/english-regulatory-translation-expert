# English Regulatory Translation Expert

把中文的生命科学与法规文本翻译成符合 FDA、ICH 术语和文风的英文。目前只支持中译英。

流程只有两步，各是一份可在界面里编辑的 system prompt：

1. **译者**（Step 1）：依据 `lifesci-translator` 技能的中译英部分，把中文原文译成英文。
2. **审校**（Step 2）：依据 `english-regulatory-reviewer` 技能，对英文译文**一次调用同时输出两个版本**：
   - *Minimalist Correction*：只改语法与拼写，显示在左侧 Step 2 面板；
   - *Polished Academic English*：学术化润色，是最终译文，显示在右侧对话里，复制按钮复制的也是它。

## 设计要点

- 审校只收到英文译文，**看不到中文原文**，像老师批改学生作业一样只改语言，不核对原意。这是有意的设计；代价是译者译错意思时，审校发现不了。以后如果想让审校看到原文，只需要改 `agent_core.py` 里第 2 步的 user 消息和审校 prompt。
- 原文里的“￼”（U+FFFC，复制粘贴常带进来的标签替身）会在发送给模型之前删除，并合并由此留下的连续空格；导出记录里保存的仍是输入的原样。
- 审校输出必须保留 `1. Minimalist Correction` 与 `2. Polished Academic English` 两个标题，程序靠它们拆分；拆不开时会报错，并把模型原始输出记入导出记录。
- 与 `english-regulatory-reviewer` 技能原文相比只有两处改动：去掉“改动处加粗”的要求（输出纯文本），并增加“保持换行与段落结构”一条。
- 温度固定为 0.3，不传 `top_p`。

## 方案

界面里的“方案”是译者与审校两份完整 prompt 的组合，在【⚙️ 设置】里选择、编辑、另存。内置方案“标准版”可以修改；保存后的内容会覆盖内置内容，设置里有按钮可以恢复。配置写入 `config.json`（不含 API 密钥，已在 `.gitignore` 中）。

## 评估记录

设置里的“导出评估记录”：每次调用（成功或失败）都会追加一条记录，回溯重译与编辑原文不会删除旧的运行；Markdown 按原文分组编号，附各版本 prompt 全文，便于对比。记录只在当前会话里，不落盘、不含密钥。

## 运行

```bash
pip install -r requirements.txt
streamlit run app.py
```

API 密钥在设置里输入（只保留在当前会话），或写进 Streamlit Secrets：

```toml
[api_keys]
"Gemini 原生接口 (默认)" = "你的密钥"
```

## 测试

```bash
python -m unittest discover -s tests
```

## 待办

- 支持英译中（需要审校 prompt 的中文版本）。
- 对比不同模型。
- 把“原版”之类的旧项目遗留设计清理干净时，注意 `config.json` 的 `profile_schema`（当前为 1）。
