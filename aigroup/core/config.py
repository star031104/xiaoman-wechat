"""读取配置：先读这个渠道的示例配置作为默认值，再用 config.yaml 覆盖；${名字} 从 .env 里取。

- CODE：仓库目录（config.wechat.example.yaml、自带的人设模板在这）
- ROOT：她的家（config.yaml、.env、data/、你自己的人设）。环境变量 AIGROUP_HOME 指定
  （python run.py 会设成 home/wechat）；没指定就是 CODE
- 渠道：这个仓库只有微信群（wechat）
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

CODE = Path(__file__).resolve().parents[2]          # aigroup/core/config.py → 仓库目录
ROOT = Path(os.environ["AIGROUP_HOME"]).expanduser().resolve() if os.environ.get("AIGROUP_HOME") else CODE
CHANNELS = ("wechat",)


def example_path(channel: str) -> Path:
    return CODE / f"config.{channel}.example.yaml"


def pick_channel(over: dict) -> str:
    return "wechat"


_CH = {"name": "微信群", "id": "群友编号"}


def set_channel(ch: str):
    """GroupChat 启动时调一次：提示词里说“QQ 群 / 微信群”、“QQ号 / 群友编号”跟着渠道走。"""
    _CH.update(name="QQ 群", id="QQ号") if ch == "qq" else _CH.update(name="微信群", id="群友编号")


def chan_name() -> str:
    return _CH["name"]


def id_label() -> str:
    return _CH["id"]


def chan(cfg) -> dict:
    """这个渠道的那一段配置（qq: 或 wechat:）。"""
    return dict(cfg.get(cfg.get("channel") or "wechat") or {})


def chan_group(cfg) -> dict:
    """群聊设置：qq.group 或 wechat.group。"""
    return dict(chan(cfg).get("group") or {})


class Cfg(dict):
    """支持 cfg.llm.model 这种点号访问的字典。"""

    def __getattr__(self, item):
        try:
            v = self[item]
        except KeyError as e:
            raise AttributeError(item) from e
        return Cfg(v) if isinstance(v, dict) and not isinstance(v, Cfg) else v

    def __setattr__(self, key, value):
        self[key] = value


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _wrap(d):
    if isinstance(d, dict):
        return Cfg({k: _wrap(v) for k, v in d.items()})
    return d


def load_config(path: str | os.PathLike | None = None, channel: str | None = None) -> Cfg:
    path = Path(path or os.environ.get("AIGROUP_CONFIG") or "config.yaml")
    if not path.is_absolute():
        path = ROOT / path
    over = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            over = yaml.safe_load(f) or {}
    ch = channel if channel in CHANNELS else pick_channel(over)
    with open(example_path(ch), encoding="utf-8") as f:
        base = yaml.safe_load(f) or {}
    from aigroup.core.secrets import read_env, substitute
    from aigroup.llm.custom import CUSTOM_FILE, load_custom, merge_custom
    merged = _merge(base, over)
    if (ROOT / CUSTOM_FILE).exists():            # 自己加的大模型接口（python run.py llm 写的）
        merged = merge_custom(merged, load_custom(ROOT))
    cfg = _wrap(substitute(merged, read_env(ROOT)))
    cfg["tz"] = ZoneInfo(cfg.timezone)
    cfg["root"] = ROOT
    cfg["code"] = CODE
    cfg["channel"] = ch
    cfg["has_user_config"] = path.exists()
    pf = str(cfg.get("persona_file") or "").replace("\\", "/")
    while pf.startswith("./"):
        pf = pf[2:]
    cfg["persona_file"] = pf
    return cfg


def resolve(cfg: Cfg, p: str) -> Path:
    """相对路径先在她的家里找，没有再到仓库里找（自带的人设模板在仓库的 persona/ 里）。"""
    q = Path(p)
    if q.is_absolute():
        return q
    mine = Path(cfg.root) / q
    if mine.exists() or not cfg.get("code"):
        return mine
    shipped = Path(cfg.code) / q
    return shipped if shipped.exists() else mine
