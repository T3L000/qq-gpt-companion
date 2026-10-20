"""Whitelisted, bounded and versioned QQ-companion numeric balance sheet.

Only existing numeric tuning knobs may change here. This cannot alter group
allowlists, model credentials, enable flags, outbound permissions or sessions.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from . import core

SPECS: tuple[dict[str, Any], ...] = (
    {"id":"ambient_probability","section":"社交参与","label":"自然插话概率",
     "description":"没有点名时被选中参与的基础概率。低频参与仍受冷却和小时上限约束。",
     "path":("engagement","ambient_probability"),"min":0.0,"max":0.30,"step":0.01,"type":"number","unit":"概率"},
    {"id":"followup_probability","section":"社交参与","label":"短期接话概率",
     "description":"刚刚真实互动过的成员在接话窗口内的参与概率；仍受冷却和小时上限约束。",
     "path":("engagement","followup_probability"),"min":0.0,"max":1.0,"step":0.01,"type":"number","unit":"概率"},
    {"id":"followup_window_seconds","section":"社交参与","label":"注意力窗口",
     "description":"成功发出消息后，在多长时间内优先考虑同一成员的跟进话题。",
     "path":("engagement","followup_window_seconds"),"min":30,"max":600,"step":10,"type":"integer","unit":"秒"},
    {"id":"ambient_cooldown_seconds","section":"社交参与","label":"主动参与冷却",
     "description":"任意一次非定向发言被选中后至少安静多久。针对整个群生效。",
     "path":("engagement","ambient_cooldown_seconds"),"min":30,"max":3600,"step":30,"type":"integer","unit":"秒"},
    {"id":"ambient_max_per_hour","section":"社交参与","label":"每小时主动发言上限",
     "description":"只统计非定向请求；设为 0 会停用普通参与和短期接话，不影响定向问题。",
     "path":("engagement","ambient_max_per_hour"),"min":0,"max":12,"step":1,"type":"integer","unit":"次"},
    {"id":"quiet_start","section":"社交参与","label":"免打扰开始",
     "description":"本机小时制。开始和结束相同时表示不设固定免打扰时段。",
     "path":("engagement","quiet_hours",0),"min":0,"max":23,"step":1,"type":"integer","unit":"点"},
    {"id":"quiet_end","section":"社交参与","label":"免打扰结束",
     "description":"本机小时制。免打扰仅限制非定向参与。",
     "path":("engagement","quiet_hours",1),"min":0,"max":23,"step":1,"type":"integer","unit":"点"},
    {"id":"tail_seconds","section":"发言节奏","label":"连续消息静默",
     "description":"说话人停止发言多少秒后再处理，便于合并连续补充。",
     "path":("engagement","tail_seconds"),"min":0,"max":8,"step":1,"type":"integer","unit":"秒"},
    {"id":"max_collection_seconds","section":"发言节奏","label":"最长收集窗口",
     "description":"即使有人连续发言，最多延迟多久进入处理。",
     "path":("engagement","max_collection_seconds"),"min":2,"max":30,"step":1,"type":"integer","unit":"秒"},
    {"id":"max_ambient_reply_age_seconds","section":"发言节奏","label":"主动回复有效期",
     "description":"模型自动生成的非定向回复若等待太久，放弃发送而不重新生成；定向回复不受此限制。",
     "path":("engagement","max_ambient_reply_age_seconds"),"min":30,"max":300,"step":10,"type":"integer","unit":"秒"},
    {"id":"max_prompt_messages","section":"对话上下文","label":"携带消息条数",
     "description":"每次模型最多参考本群多少条最近消息，避免不同 QQ 群共享上下文。",
     "path":("engagement","max_prompt_messages"),"min":4,"max":60,"step":1,"type":"integer","unit":"条"},
    {"id":"max_reply_characters","section":"对话上下文","label":"最大回复长度",
     "description":"发送队列截断上限；Codex 当前更严格，最多写入 500 字。",
     "path":("engagement","max_reply_characters"),"min":50,"max":650,"step":10,"type":"integer","unit":"字"},
    {"id":"codex_cli_timeout_seconds","section":"模型额度","label":"单次模型超时",
     "description":"超时进入 UNKNOWN，绝不自动重试以免重复消耗额度。",
     "path":("provider","codex_cli_timeout_seconds"),"min":20,"max":150,"step":5,"type":"integer","unit":"秒"},
    {"id":"codex_cli_hourly_limit","section":"模型额度","label":"模型每小时预算",
     "description":"最多允许的调用次数，不等于 ChatGPT 套餐的官方剩余额度。",
     "path":("provider","codex_cli_hourly_limit"),"min":1,"max":12,"step":1,"type":"integer","unit":"次"},
    {"id":"codex_cli_daily_limit","section":"模型额度","label":"模型每日预算",
     "description":"本地滚动 24 小时预算，用于减少无人值守额度消耗。",
     "path":("provider","codex_cli_daily_limit"),"min":1,"max":48,"step":1,"type":"integer","unit":"次"},
    {"id":"codex_cli_min_interval_seconds","section":"模型额度","label":"模型调用间隔",
     "description":"上一条请求开始后至少经过多少秒才允许下一次调用。",
     "path":("provider","codex_cli_min_interval_seconds"),"min":30,"max":3600,"step":30,"type":"integer","unit":"秒"},
)
BY_ID = {item["id"]:item for item in SPECS}
PRESETS = {
    "潜水":{"ambient_probability":0.01,"followup_probability":0.18,"ambient_cooldown_seconds":600,
            "ambient_max_per_hour":1,"max_reply_characters":250},
    "日常":{"ambient_probability":0.05,"followup_probability":0.42,"ambient_cooldown_seconds":180,
            "ambient_max_per_hour":4,"max_reply_characters":400},
    "活跃":{"ambient_probability":0.12,"followup_probability":0.55,"ambient_cooldown_seconds":120,
            "ambient_max_per_hour":6,"max_reply_characters":400},
}


def _get_path(config: dict, path: tuple) -> Any:
    result: Any = config
    for key in path:
        result = result[key]
    return result


def _set_path(config: dict, path: tuple, value: Any) -> None:
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def version(config: dict) -> str:
    # Version the WHOLE config: prevent a tuning write from overwriting a
    # concurrent provider switch, allowlist update or outbound permission.
    serialized=json.dumps(config,sort_keys=True,ensure_ascii=False,separators=(",",":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def snapshot(root=core.ROOT) -> dict:
    with core._DB_LOCK:
        cfg=core.config(root)
        defaults=core.default_config()
        items=[]
        for spec in SPECS:
            item={k:v for k,v in spec.items() if k!="path"}
            try:
                item["value"]=_get_path(cfg,spec["path"])
            except KeyError:
                item["value"]=_get_path(defaults,spec["path"])
            items.append(item)
        return {"version":version(cfg),"items":items,
                "presets":PRESETS,"provider_mode":cfg.get("provider",{}).get("mode","unknown"),
                "outbound_enabled":bool(cfg.get("outbound_groups")),
                "notes":"仅编辑数值，不能开启群发、修改白名单或切换模型；修改立即作用于新事件。"}


def update(values: dict, expected_version: str, root=core.ROOT) -> dict:
    if not isinstance(values,dict) or not values:
        raise ValueError("Empty numeric change set")
    if len(values)>len(SPECS):
        raise ValueError("Too many numeric changes")
    if not isinstance(expected_version,str) or len(expected_version)!=16:
        raise ValueError("Missing or invalid configuration version")
    for name,val in values.items():
        spec=BY_ID.get(name)
        if spec is None:
            raise ValueError("Only whitelisted numeric settings may be changed")
        if isinstance(val,bool) or not isinstance(val,(int,float)):
            raise ValueError("A numeric setting must be a number")
        if spec["type"]=="integer" and (not isinstance(val,int) or isinstance(val,bool)):
            raise ValueError("An integer setting must be a whole number")
        if val<spec["min"] or val>spec["max"]:
            raise ValueError("Numeric value outside safe bounds")
        tick=round((val-spec["min"])/spec["step"])
        if abs(spec["min"]+tick*spec["step"]-val)>1e-7:
            raise ValueError("Numeric value does not match its step")
    with core._DB_LOCK:
        original=core.config(root)
        if version(original)!=expected_version:
            raise ValueError("Configuration changed elsewhere; refresh the sheet first")
        replacement=copy.deepcopy(original)
        for name,value in values.items():
            _set_path(replacement,BY_ID[name]["path"],value)
        e=replacement["engagement"]
        p=replacement["provider"]
        if e["tail_seconds"]>e["max_collection_seconds"]:
            raise ValueError("Silent collection delay cannot exceed maximum window")
        defaults=core.default_config()["provider"]
        if p.get("codex_cli_hourly_limit",defaults["codex_cli_hourly_limit"])>p.get("codex_cli_daily_limit",defaults["codex_cli_daily_limit"]):
            raise ValueError("Hourly model budget must not exceed daily budget")
        core.save_config(replacement,root)
    return snapshot(root)
