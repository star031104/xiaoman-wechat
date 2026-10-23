"""密钥管理：API key、bot token 不再明文写在 config.yaml 里。

- 密钥放在项目根目录的 .env 文件（KEY=值，一行一个）。config.yaml 里写 "${MY_MODEL_KEY}" 这样的引用。
- 第一次启动时自动搬家：发现 config.yaml 里有明文密钥，就挪进 .env，并把 config.yaml 里那一项改成引用；
  只改那几行，注释和格式都保留；改完重新读一遍核对，对不上就原样恢复。
- 日志里如果意外出现这些密钥（比如报错信息里带了 URL），会被替换成 ***。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("aigroup.secrets")

REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


# ---------------- .env ----------------
def env_path(root: Path) -> Path:
    return Path(root) / ".env"


def read_env(root: Path) -> dict[str, str]:
    p = env_path(root)
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def write_env(root: Path, new: dict[str, str]):
    """追加 / 更新 .env 里的几项（别的行原样保留）。"""
    p = env_path(root)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else \
        ["# 她的密钥。不要发给别人、不要上传到网上。config.yaml 里用 ${名字} 引用这里的值。"]
    done = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in new:
            lines[i] = f"{k}={new[k]}"
            done.add(k)
    lines += [f"{k}={v}" for k, v in new.items() if k not in done]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def substitute(obj, env: dict[str, str]):
    """把配置里所有 "${NAME}" 换成 .env / 环境变量里的值（找不到就换成空字符串）。"""
    if isinstance(obj, dict):
        return obj.__class__({k: substitute(v, env) for k, v in obj.items()})
    if isinstance(obj, list):
        return [substitute(v, env) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        return REF.sub(lambda m: os.environ.get(m.group(1), env.get(m.group(1), "")), obj)
    return obj


# ---------------- 哪些是密钥 ----------------
def secret_paths(cfg: dict) -> list[tuple[tuple[str, ...], str]]:
    """返回 [(配置路径, 建议的变量名)]。"""
    out: list[tuple[tuple[str, ...], str]] = []
    for k in (cfg.get("keys") or {}):
        out.append((("keys", k), f"{re.sub(r'[^A-Za-z0-9]', '_', k).upper()}_KEY"))
    for name, p in ((cfg.get("llm") or {}).get("profiles") or {}).items():
        if isinstance(p, dict) and "api_key" in p:
            out.append((("llm", "profiles", name, "api_key"), f"LLM_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}_API_KEY"))
    return out


def _get(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


PLACEHOLDERS = {"", "123456:ABC...", "ollama", "none", "null"}


def is_plain_secret(v) -> bool:
    return isinstance(v, str) and v.strip() not in PLACEHOLDERS and "${" not in v and len(v.strip()) >= 8


def secret_values(cfg: dict) -> list[str]:
    """当前生效的所有密钥值（给日志打码用）。"""
    vals = []
    for path, _ in secret_paths(cfg):
        v = _get(cfg, path)
        if isinstance(v, str) and len(v) >= 8:
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


# ---------------- 自动搬家 ----------------
def migrate(root: Path) -> list[str]:
    """把 config.yaml 里的明文密钥挪进 .env。返回挪了哪些（变量名）。失败时不改任何文件。"""
    import yaml
    root = Path(root)
    cfg_path = root / "config.yaml"
    if not cfg_path.exists():
        return []
    text = cfg_path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001
        return []
    lines = text.split("\n")
    moved: dict[str, str] = {}
    for path, var in secret_paths(raw):
        v = _get(raw, path)
        if not is_plain_secret(v):
            continue
        leaf = re.escape(path[-1])
        pat = re.compile(rf"^(\s*{leaf}\s*:\s*)([\"']?){re.escape(v)}\2(\s*(#.*)?)$")
        hits = [i for i, line in enumerate(lines) if pat.match(line)]
        if len(hits) != 1:                    # 找不到或不唯一：这一项不动，免得改错
            continue
        i = hits[0]
        lines[i] = pat.sub(lambda m: f'{m.group(1)}"${{{var}}}"{m.group(3)}', lines[i])
        moved[var] = v
    if not moved:
        return []
    new_text = "\n".join(lines)
    # 核对：改完以后读出来的值必须和原来一模一样
    env = {**read_env(root), **moved}
    after = substitute(yaml.safe_load(new_text) or {}, env)
    before = substitute(raw, env)                     # 原来就是 ${...} 引用的，按同样的方式展开再比
    for path, _ in secret_paths(raw):
        if _get(after, path) != _get(before, path):
            log.warning("密钥搬家核对没通过，config.yaml 保持原样")
            return []
    write_env(root, moved)
    tmp = cfg_path.with_suffix(".yaml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, cfg_path)
    return list(moved)


# ---------------- 日志打码 ----------------
class Redactor(logging.Filter):
    def __init__(self, values: list[str]):
        super().__init__()
        self.values = [v for v in values if v and len(v) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.values:
            return True
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        red = msg
        for v in self.values:
            if v in red:
                red = red.replace(v, v[:3] + "***")
        if red != msg:
            record.msg, record.args = red, ()
        if record.exc_info and record.exc_info[1] is not None:
            s = str(record.exc_info[1])
            if any(v in s for v in self.values):
                record.exc_text = "（异常信息里有密钥，已隐藏）"
        return True


def install_redaction(values: list[str]):
    f = Redactor(values)
    for h in logging.getLogger().handlers:
        if not any(isinstance(x, Redactor) for x in h.filters):
            h.addFilter(f)
    return f
