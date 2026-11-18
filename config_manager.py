# config_manager.py
import os
import copy
import json
import streamlit as st
from prompts import DEFAULT_SCHEME_NAME, BUILTIN_SCHEMES

# ==================== 方案（译者 + 审校 两份完整 prompt）的存储格式 ====================
#   skills = {"profile_schema": 1, "schemes": {名称: {"translator": ..., "reviewer": ...}}, "active_scheme": 名称}
# 内置方案“标准版”始终存在；用户保存过同名方案时，以保存的为准。
# 配置里 profile_schema 不是 1 的方案（例如别的项目留下的文件）一律忽略，避免把不兼容的内容当成 prompt。
PROFILE_SCHEMA_VERSION = 1


def _scheme(translator="", reviewer=""):
    return {"translator": translator or "", "reviewer": reviewer or ""}


def _build_schemes(skills_cfg):
    """内置方案放在最前面；文件里保存的方案（格式版本必须匹配）追加在后面，同名时以保存的为准。"""
    saved = {}
    if (skills_cfg or {}).get("profile_schema") == PROFILE_SCHEMA_VERSION:
        saved = {n: s for n, s in ((skills_cfg or {}).get("schemes") or {}).items() if isinstance(s, dict)}
    result = {name: dict(s) for name, s in BUILTIN_SCHEMES.items()}
    for name, s in saved.items():
        result[name] = s
    return {name: _scheme(s.get("translator"), s.get("reviewer")) for name, s in result.items()}


def _valid_active(name, schemes):
    """没有保存过、或激活项指向不存在的方案时，回退到内置的标准版。"""
    return name if name in schemes else DEFAULT_SCHEME_NAME

DEFAULT_ENV_NAME = "Gemini 原生接口 (默认)"

# ==================== API 密钥：不落盘 ====================
# 密钥只有两个来源：1) 设置界面里输入（只保留在当前浏览器会话里）；2) Streamlit Secrets。
# config.json 里不再保存、也不再读取密钥（旧版本会把密钥明文写进该文件）。

def _without_keys(config_data):
    """写入文件前强制清空所有接口的密钥，调用方无法绕过。"""
    data = copy.deepcopy(config_data)
    for env in data.get("api_config", {}).get("envs", {}).values():
        if isinstance(env, dict):
            env["key"] = ""
    return data

def secret_api_key(env_name):
    """从 Streamlit Secrets 读取密钥；没有配置则返回空字符串。
    优先读 [api_keys] 表里与接口名同名的条目；默认接口也可以用顶层的 GEMINI_API_KEY。"""
    try:
        table = st.secrets.get("api_keys", {})
        if table and table.get(env_name):
            return str(table[env_name]).strip()
        if env_name == DEFAULT_ENV_NAME and st.secrets.get("GEMINI_API_KEY"):
            return str(st.secrets["GEMINI_API_KEY"]).strip()
    except Exception:
        pass  # 没有配置任何 Secrets 时，访问 st.secrets 会抛出异常
    return ""

def resolve_api_key_with_source(env_name):
    """返回 (密钥, 来源)。来源为 "session"（本次会话输入）、"secrets" 或 ""（没有密钥）。"""
    typed = (st.session_state.api_envs.get(env_name) or {}).get("key", "")
    if typed:
        return typed, "session"
    secret = secret_api_key(env_name)
    if secret:
        return secret, "secrets"
    return "", ""

def resolve_api_key(env_name):
    return resolve_api_key_with_source(env_name)[0]

def api_key_source(env_name):
    return resolve_api_key_with_source(env_name)[1]

def load_config():
    default_cfg = {
        "api_config": {
            "active_env": "Gemini 原生接口 (默认)",
            "envs": {"Gemini 原生接口 (默认)": {"url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key": ""}},
            "current_model": "gemini-2.5-flash"
        },
        "skills": {}
    }
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return default_cfg

def save_config(config_data):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(_without_keys(config_data), f, ensure_ascii=False, indent=4)

def init_session_state():
    config = load_config()
    api_cfg = config.get("api_config", {})
    if "api_envs" not in st.session_state:
        envs_cfg = api_cfg.get("envs", {})
        # 文件里的密钥一律不使用；若发现旧版本留下的明文密钥，界面会提示（保存配置后文件中的密钥会被清除）
        st.session_state.api_envs = {name: {**(env or {}), "key": ""} for name, env in envs_cfg.items()}
        st.session_state.plaintext_key_warning = any((env or {}).get("key") for env in envs_cfg.values())
    if "active_env" not in st.session_state: st.session_state.active_env = api_cfg.get("active_env", "Gemini 原生接口 (默认)")
    if "selected_model" not in st.session_state: st.session_state.selected_model = api_cfg.get("current_model", "gemini-2.5-flash")
    if "available_models" not in st.session_state: st.session_state.available_models = [st.session_state.selected_model]
    if "model_history" not in st.session_state: st.session_state.model_history = [{"id": st.session_state.selected_model, "count": 1}]

    skills_cfg = config.get("skills", {})
    if "schemes" not in st.session_state: st.session_state.schemes = _build_schemes(skills_cfg)
    if "active_scheme" not in st.session_state:
        active = skills_cfg.get("active_scheme") if skills_cfg.get("profile_schema") == PROFILE_SCHEMA_VERSION else None
        st.session_state.active_scheme = _valid_active(active, st.session_state.schemes)

    if "messages" not in st.session_state: st.session_state.messages = []
    if "editing_idx" not in st.session_state: st.session_state.editing_idx = None

def record_model_usage(model_name: str):
    model_name = model_name.strip()
    if not model_name: return
    new_list = [item for item in st.session_state.model_history if item["id"] != model_name]
    existing = next((item for item in st.session_state.model_history if item["id"] == model_name), {"count": 0})
    new_list.insert(0, {"id": model_name, "count": existing["count"] + 1})
    st.session_state.model_history = new_list
    st.session_state.selected_model = model_name
