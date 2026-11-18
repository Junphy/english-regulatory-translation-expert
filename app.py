import html
import time

import openai
import streamlit as st

# 从拆分模块中导入功能
from config_manager import init_session_state, record_model_usage, resolve_api_key, api_key_source
from ui_components import inject_custom_css, show_settings_dialog, render_action_icons, install_page_script
from agent_core import run_two_step, StepError, EmptyCompletionError, UnsupportedInputError, ReviewFormatError
from history_export import log_run

APP_TITLE = "English Regulatory Translation Expert"
KEY_SOURCE_LABELS = {"session": "（本次会话输入）", "secrets": "（Secrets）"}


def html_text(text):
    """转义后再拼进 HTML。左侧两个面板用 HTML 显示文字，不转义的话，
    译文里形如 <obj id="1"/> 的标签或占位符会被浏览器当作 HTML 标签而不显示。"""
    return "<br>".join(html.escape(text or "", quote=False).split("\n"))


def _openai_exc(name):
    return getattr(openai, name, ())  # 空元组在 isinstance 中不匹配任何类型


def describe_error(exc, api_key):
    """把异常整理成一段中文说明；说明里不会出现 API 密钥。"""
    prefix = ""
    if isinstance(exc, UnsupportedInputError):
        return str(exc)
    if isinstance(exc, StepError):
        if isinstance(exc.cause, ReviewFormatError):
            return f"第 {exc.step} 步（{exc.label}）的输出格式不符：{exc.cause}（模型原始输出已记入导出记录。可点击“回溯重译”重试，或换一个模型。）"
        prefix = f"第 {exc.step} 步（{exc.label}）调用失败。"
        exc = exc.cause
    detail = str(exc).strip()
    if api_key:
        detail = detail.replace(api_key, "***")
    if len(detail) > 300:
        detail = detail[:300] + "……"
    if isinstance(exc, EmptyCompletionError):
        return f"{prefix}{detail}"
    if isinstance(exc, (_openai_exc("AuthenticationError"), _openai_exc("PermissionDeniedError"))):
        hint = "接口拒绝了凭据，或没有访问权限（HTTP 401/403）。请检查密钥是否正确、接口地址是否匹配。"
    elif isinstance(exc, _openai_exc("RateLimitError")):
        hint = "触发限流或额度不足（HTTP 429）。请稍后重试，或检查账户额度。"
    elif isinstance(exc, _openai_exc("BadRequestError")):
        hint = "接口拒绝了请求（HTTP 400）。可能是模型名不正确，或接口不接受某个请求参数，也可能是输入被拦截。"
    elif isinstance(exc, _openai_exc("NotFoundError")):
        hint = "找不到模型或接口地址（HTTP 404）。请检查模型名与接口地址。"
    elif isinstance(exc, _openai_exc("APIConnectionError")):
        hint = "无法连接到接口（网络问题，或接口地址不正确）。"
    else:
        hint = ""
    return f"{prefix}{hint + ' ' if hint else ''}原始信息：{type(exc).__name__}: {detail}"


def run_translation(text):
    """调用两步翻译。成功返回 (结果, None)，失败返回 (None, 中文错误说明)。
    每次调用（成功或失败）都会追加一条运行记录，供设置里的“导出评估记录”使用；
    失败时，已经完成的内容（如第 1 步的译文、审校的原始输出）也会记进去。"""
    env_name = st.session_state.active_env
    env = st.session_state.api_envs.get(env_name, {})
    key = resolve_api_key(env_name)
    scheme_name = st.session_state.active_scheme
    scheme = st.session_state.schemes[scheme_name]
    started = time.monotonic()
    result, err, partial = None, None, None
    try:
        result = run_two_step(
            text, st.session_state.selected_model, key, env.get("url", ""),
            scheme["translator"],  # 第 1 步 译者的 system prompt
            scheme["reviewer"],    # 第 2 步 审校的 system prompt
        )
    except Exception as exc:  # noqa: BLE001  任何失败都转成界面提示，不让页面崩溃
        err = describe_error(exc, key)
        partial = getattr(exc, "partial", None)
    log_run(text, scheme_name, scheme, st.session_state.selected_model, env_name,
            result, err, time.monotonic() - started, partial)
    return (result, None) if err is None else (None, err)


def make_message(user_text, result):
    """把一次成功的结果整理成对话里的一条记录。"""
    translation, minimal, polished, direction = result
    return {"user": user_text, "translation": translation, "minimal": minimal, "polished": polished,
            "model": st.session_state.selected_model, "direction": direction, "scheme": st.session_state.active_scheme}


# ==================== 1. 页面初始化与 CSS 注入 ====================
st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded"
)
inject_custom_css()
init_session_state()

# ==================== 2. 后端处理 URL 传来的操作指令 ====================
if "action" in st.query_params and "msg_idx" in st.query_params:
    action_type = st.query_params["action"]
    msg_idx = int(st.query_params["msg_idx"])
    st.query_params.clear()

    if action_type == "edit":
        st.session_state.editing_idx = msg_idx
        st.rerun()
    elif action_type == "rewind":
        st.session_state.pending_rewind_idx = msg_idx
        st.rerun()

# ==================== 3. 左侧栏：标题与 Step 1 / Step 2 两个面板 ====================
active_turn = st.session_state.messages[-1] if len(st.session_state.messages) > 0 else None
turn_ok = bool(active_turn) and not active_turn.get("error")
# 面板里显示这一轮实际使用的方案；没有记录时显示当前选中的方案
turn_scheme = html.escape((active_turn.get("scheme") if turn_ok else None) or st.session_state.active_scheme, quote=False)


def panel_body(key, empty_hint):
    if turn_ok:
        return html_text(active_turn[key])
    if active_turn:
        return "<span style='color:gray;font-size:14px;'>本轮调用失败，请查看右侧的错误提示。</span>"
    return f"<span style='color:gray;font-size:14px;'>{empty_hint}</span>"


with st.sidebar:
    st.markdown(f'<div class="app-title" title="{APP_TITLE}">🧬 {APP_TITLE}</div>', unsafe_allow_html=True)
    key_source = api_key_source(st.session_state.active_env)
    st.caption(f"{'🟢' if key_source else '🔴'} 接口: `{st.session_state.active_env}`{KEY_SOURCE_LABELS.get(key_source, '')}")
    st.markdown(f'''
    <div class="step-card">
        <div class="sticky-header">
            <strong>Step 1 · 初译（译者）</strong>
            <p>🔵 方案：{turn_scheme}</p>
        </div>
        <div class="step-body">
            {panel_body("translation", "暂无内容，发送中文文本后在此展示第一步初译。")}
        </div>
    </div>
    <div class="step-card">
        <div class="sticky-header">
            <strong>Step 2 · 最小修改（Minimalist Correction）</strong>
        </div>
        <div class="step-body">
            {panel_body("minimal", "暂无内容，发送中文文本后在此展示审校的最小修改版本。")}
        </div>
    </div>
    ''', unsafe_allow_html=True)

# ==================== 4. 右侧主区：顶栏 + 对话流（只用整页这一条滚动条） ====================
# 这一行（设置、模型）在桌面宽度下由 CSS 固定到页眉行（设置按钮在最左），窄屏则吸顶显示
top_settings, top_model = st.columns([1.3, 3])
with top_settings:
    st.markdown('<span class="topbar-marker"></span>', unsafe_allow_html=True)  # 供 CSS 找到这一行
    if st.button("⚙️ 设置"):
        show_settings_dialog()
with top_model:
    hist_ids = [m["id"] for m in st.session_state.model_history]
    combined_models = list(dict.fromkeys(hist_ids + st.session_state.available_models))
    current_idx = combined_models.index(st.session_state.selected_model) if st.session_state.selected_model in combined_models else 0
    sel_model = st.selectbox("当前选择模型", options=combined_models, index=current_idx, label_visibility="collapsed")
    if sel_model != st.session_state.selected_model:
        record_model_usage(sel_model)
        st.rerun()

if st.session_state.get("plaintext_key_warning"):
    st.warning("检测到 config.json 里保存过明文密钥，本版本不再读取它。请在【⚙️ 设置】里重新输入密钥（仅本次会话有效），或改用 Secrets 长期保存；点击“保存全部配置并生效”后，文件中的明文密钥会被清除。")

chat_container = st.container()

with chat_container:
    for i, msg in enumerate(st.session_state.messages):
        # 用户气泡
        with st.chat_message("user"):
            if st.session_state.editing_idx == i:
                edited_val = st.text_area("编辑原文", value=msg["user"], key=f"edit_area_{i}", label_visibility="collapsed")
                ec1, ec2, _ = st.columns([1, 1, 10])
                with ec1:
                    if st.button("✔️ 保存修改", key=f"save_edit_{i}"):
                        with st.spinner(f"⏳ 正在调用 {st.session_state.selected_model} 执行法规翻译..."):
                            result, err = run_translation(edited_val)
                        if err:
                            st.error(err)  # 失败时保持编辑状态，修改过的文字不会丢
                        else:
                            st.session_state.messages[i] = make_message(edited_val, result)
                            st.session_state.messages = st.session_state.messages[:i + 1]
                            st.session_state.editing_idx = None
                            st.rerun()
                with ec2:
                    if st.button("✖️ 取消", key=f"cancel_edit_{i}"):
                        st.session_state.editing_idx = None
                        st.rerun()
            else:
                st.markdown(msg["user"])
                render_action_icons(msg["user"], i, is_user=True)

        # Agent 回复气泡
        with st.chat_message("assistant"):
            if msg.get("error"):
                st.error(msg["error"])
                st.caption("原文已保留。可点击上方气泡下的“回溯重译”图标重试，或先在【⚙️ 设置】里处理问题。")
            else:
                direction_note = f" | 方向: {msg['direction']}" if msg.get("direction") else ""
                st.caption(f"Model: `{msg.get('model', st.session_state.selected_model)}` | 方案: `{msg.get('scheme', st.session_state.active_scheme)}`{direction_note} · Turn #{i + 1} · 最终译文（Polished Academic English）")
                st.markdown(msg["polished"].replace("\n", "  \n"))  # 单个换行在 markdown 里会被合并，这里改成硬换行，多行清单才不会挤成一行
                render_action_icons(msg["polished"], i, is_user=False)

    # 处理等待执行的回溯 (Rewind) 任务
    if "pending_rewind_idx" in st.session_state:
        rewind_idx = st.session_state.pending_rewind_idx
        del st.session_state["pending_rewind_idx"]
        target_text = st.session_state.messages[rewind_idx]["user"]
        with st.spinner(f"⏳ 正在调用 {st.session_state.selected_model} 执行法规翻译..."):
            result, err = run_translation(target_text)
        if err:
            if st.session_state.messages[rewind_idx].get("error"):
                st.session_state.messages[rewind_idx]["error"] = err  # 原来就是失败记录：更新错误说明
                st.rerun()
            else:
                st.error(err)  # 原来有结果：保持原结果不变，只提示失败
        else:
            st.session_state.messages = st.session_state.messages[:rewind_idx]
            st.session_state.messages.append(make_message(target_text, result))
            st.rerun()

    st.markdown('<div id="bottom-anchor"></div>', unsafe_allow_html=True)

# ----------------- 5. 底部输入流处理 -----------------
prompt = st.chat_input("输入需要翻译的中文临床/法规文本...")
install_page_script()
if prompt:
    if not resolve_api_key(st.session_state.active_env):
        st.error("❌ 当前选中的接口尚未配置凭据，请点击右上角【⚙️ 设置】输入密钥，或在 Secrets 中配置。")
    else:
        record_model_usage(st.session_state.selected_model)
        st.session_state.pending_new_prompt = prompt
        st.rerun()

if "pending_new_prompt" in st.session_state:
    pending_prompt = st.session_state.pending_new_prompt
    del st.session_state["pending_new_prompt"]

    with chat_container:
        with st.chat_message("user"):
            st.markdown(pending_prompt)
        with st.chat_message("assistant"):
            with st.spinner(f"⏳ 正在调用 {st.session_state.selected_model} 执行法规翻译..."):
                result, err = run_translation(pending_prompt)
            if err:
                # 失败也保留原文，便于回溯重试
                st.session_state.messages.append({"user": pending_prompt, "error": err, "model": st.session_state.selected_model})
            else:
                st.session_state.messages.append(make_message(pending_prompt, result))
            st.rerun()
