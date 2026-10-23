"""自定义大模型接口：给一个地址和 Key → 测连通 → 列出模型让你选 → 试聊一句 → 加进路由。

    python run.py llm        （或双击 scripts/llm.bat；setup 向导里也可以顺便加）

任何 OpenAI 兼容的接口都行（云端的、本机跑的都可以），程序里不预置任何接口。
加的接口写在 config.llm.yaml（不动你的 config.yaml，注释都还在），Key 写进 .env。
config.llm.yaml 在 config.yaml 之后读：里面的 llm.profiles 是追加，llm.routes 里写了的用途整个替换。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

from aigroup.core.config import ROOT

CUSTOM_FILE = "config.llm.yaml"
# 不是聊天模型的（向量、重排、语音、画图）：列模型时默认藏起来
NON_CHAT = re.compile(r"embed|rerank|bge-|tts|whisper|speech|audio|asr|image|vision-?embed|dall|flux|stable-?diffusion|"
                      r"sdxl|kolors|moderation|cogview|video|wan2|hunyuan-?video", re.I)


# ---------------- 纯函数（好测） ----------------
def normalize_url(u: str) -> str:
    u = (u or "").strip().strip('"').strip("'").rstrip("/")
    if u and not re.match(r"^https?://", u):
        u = ("http://" if re.match(r"^(localhost|127\.|192\.168\.|10\.)", u) else "https://") + u
    for tail in ("/chat/completions", "/completions", "/models"):
        if u.endswith(tail):
            u = u[: -len(tail)]
    return u


def url_candidates(u: str) -> list[str]:
    """用户填的地址没带 /v1 的话，两个都试一下。"""
    u = normalize_url(u)
    out = [u]
    if not re.search(r"/v\d+[a-z]*$|/api/paas/v4$|/compatible-mode/v1$", u):
        out.append(u + "/v1")
    return out


def profile_name(base_url: str, model: str, taken: set[str] | None = None) -> str:
    host = (urlparse(base_url).hostname or "local").lower()
    host = "local" if host in ("127.0.0.1", "localhost") else (host.split(".")[-2] if host.count(".") else host)
    m = re.sub(r"[^a-z0-9]+", "_", model.lower().split("/")[-1]).strip("_")[:24]
    base = re.sub(r"_+", "_", f"my_{host}_{m}").strip("_")
    name, i = base, 2
    while taken and name in taken:
        name, i = f"{base}_{i}", i + 1
    return name


def env_var(name: str) -> str:
    return "LLM_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper() + "_API_KEY"


def chat_models(ids: list[str], show_all: bool = False) -> list[str]:
    ids = sorted(set(i for i in ids if i))
    return ids if show_all else [i for i in ids if not NON_CHAT.search(i)]


def new_routes(routes: dict, name: str, how: str, roles_bg: tuple[str, ...]) -> dict:
    """how：first（当主力）/ last（当备用）/ background（只干后台的活）。返回要写进 config.llm.yaml 的 routes。"""
    out: dict[str, list[str]] = {}

    def put(role, first):
        cur = [n for n in (routes.get(role) or (routes.get("helper") if role in ("memory", "night") else None)
                           or routes.get("chat") or []) if n != name]
        out[role] = [name] + cur if first else cur + [name]
    if how == "first":                    # 当主力：聊天和后台的活都先用它（以前只改 chat，后台还走示例里的接口）
        put("chat", True)
        for r in roles_bg:
            put(r, True)
    elif how == "last":
        put("chat", False)
    else:
        for r in roles_bg:
            put(r, True)
    return out


def load_custom(root: Path = ROOT) -> dict:
    p = Path(root) / CUSTOM_FILE
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_custom(data: dict, root: Path = ROOT):
    p = Path(root) / CUSTOM_FILE
    head = ("# 自定义的大模型接口（python run.py llm 写的；可以手改）\n"
            "# llm.profiles 追加到 config.yaml 里的接口；llm.routes 里写了的用途会整个替换 config.yaml 里的\n"
            "# Key 在 .env 里，这里只写 ${名字} 引用\n\n")
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None),
                   encoding="utf-8")
    tmp.replace(p)


def merge_custom(base: dict, custom: dict) -> dict:
    """config.yaml 读完后再叠加 config.llm.yaml。"""
    c = (custom or {}).get("llm") or {}
    if not c:
        return base
    llm = dict(base.get("llm") or {})
    llm["profiles"] = {**(llm.get("profiles") or {}), **(c.get("profiles") or {})}
    llm["routes"] = {**(llm.get("routes") or {}), **(c.get("routes") or {})}
    return {**base, "llm": llm}


# ---------------- 联网 ----------------
async def list_models(base_url: str, key: str, proxy: str = "") -> tuple[list[str], str, bool, str]:
    """返回 (模型 id 列表, 能用的地址, 是否要走代理, 出错信息)。"""
    import httpx
    errs = []
    for url in url_candidates(base_url):
        for px in ([None, proxy] if proxy else [None]):
            try:
                async with httpx.AsyncClient(timeout=20, proxy=px, trust_env=False) as c:
                    r = await c.get(url + "/models", headers={"Authorization": f"Bearer {key}"} if key else {})
                if r.status_code in (401, 403):
                    return [], url, bool(px), f"Key 不对或没有权限（HTTP {r.status_code}）：{r.text[:150]}"
                r.raise_for_status()
                data = r.json()
                items = data.get("data") if isinstance(data, dict) else data
                ids = [str(m.get("id") or m.get("name") or "") if isinstance(m, dict) else str(m) for m in items or []]
                if isinstance(data, dict) and not ids and data.get("models"):          # Ollama 原生格式
                    ids = [str(m.get("name") or m.get("model")) for m in data["models"]]
                return [i for i in ids if i], url, bool(px), ""
            except Exception as e:  # noqa: BLE001
                errs.append(f"{url}{'（走代理）' if px else ''}：{str(e)[:120]}")
    return [], normalize_url(base_url), False, "；".join(errs)


async def test_chat(base_url: str, key: str, model: str, proxy: str = "", via_proxy: bool = False,
                    extra_body: dict | None = None) -> tuple[bool, float, str]:
    """试聊一句。返回 (成功, 用时秒, 回复或出错信息)。"""
    import httpx
    from openai import AsyncOpenAI

    from aigroup.llm.client import strip_think
    http = httpx.AsyncClient(proxy=proxy, timeout=60) if via_proxy and proxy else None
    client = AsyncOpenAI(base_url=base_url, api_key=key or "none", http_client=http, timeout=60, max_retries=0)
    t0 = time.time()
    try:
        r = await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "用一句话、十个字以内跟我打个招呼。"}],
            max_tokens=200, temperature=0.7, **({"extra_body": extra_body} if extra_body else {}))
        text = strip_think(r.choices[0].message.content or "").strip()
        if not text:
            return False, time.time() - t0, "回复是空的（可能是推理模型把字数都用在思考上了）"
        return True, time.time() - t0, text
    except Exception as e:  # noqa: BLE001
        return False, time.time() - t0, str(e)[:300]
    finally:
        await client.close()


async def probe_model(base_url: str, key: str, model: str, proxy: str = "",
                      via_proxy: bool = False) -> tuple[bool, float, str, dict | None]:
    """试聊；推理模型回复是空的时，换几种“关掉思考”的参数再试。返回 (成功, 用时, 回复/错误, 要带的 extra_body)。"""
    ok, secs, text = await test_chat(base_url, key, model, proxy, via_proxy)
    if not ok and ("空的" in text or "think" in text.lower()):
        for eb in ({"enable_thinking": False}, {"thinking": {"type": "disabled"}}, {"reasoning_effort": "none"},
                   {"think": False}):
            ok2, secs2, text2 = await test_chat(base_url, key, model, proxy, via_proxy, eb)
            if ok2:
                return ok2, secs2, text2, eb
    return ok, secs, text, None


def save_profile(cfg, *, url: str, key: str, model: str, name: str, how: str, roles_bg: tuple[str, ...],
                 via_proxy: bool = False, extra: dict | None = None, root: Path = ROOT) -> tuple[str, dict]:
    """把一个接口写进 config.llm.yaml（Key 写 .env）。返回 (名字, 写进去后的 routes)。"""
    from aigroup.core.secrets import write_env
    custom = load_custom(root)
    llm_c = custom.setdefault("llm", {})
    profiles = llm_c.setdefault("profiles", {})
    taken = set((cfg.get("llm") or {}).get("profiles") or {}) | set(profiles)
    name = re.sub(r"[^A-Za-z0-9_]", "_", (name or "").strip()) or profile_name(url, model, taken)
    prof: dict = {"base_url": normalize_url(url), "model": model}
    if key:
        var = env_var(name)
        write_env(root, {var: key})
        prof["api_key"] = f"${{{var}}}"
    else:
        prof["api_key"] = "none"
    if extra:
        prof["extra_body"] = extra
    if via_proxy:
        prof["use_proxy"] = True
    if re.search(r"127\.0\.0\.1|localhost", url):
        prof["timeout"] = 180                     # 本机模型第一次加载慢
    profiles[name] = prof
    routes = dict((cfg.get("llm") or {}).get("routes") or {})
    routes.update(llm_c.get("routes") or {})
    llm_c["routes"] = {**(llm_c.get("routes") or {}), **new_routes(routes, name, how, roles_bg)}
    save_custom(custom, root)
    return name, llm_c["routes"]


def remove_profile(name: str, root: Path = ROOT) -> bool:
    custom = load_custom(root)
    profiles = ((custom.get("llm") or {}).get("profiles") or {})
    if name not in profiles:
        return False
    profiles.pop(name)
    routes = custom["llm"].get("routes") or {}
    for r in list(routes):
        routes[r] = [x for x in routes[r] if x != name]
        if not routes[r]:
            routes.pop(r)
    save_custom(custom, root)
    return True


# ---------------- 交互 ----------------
def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    try:
        if secret:
            import getpass
            v = getpass.getpass(f"{prompt}（输入时不显示）：")
        else:
            v = input(f"{prompt}{f' [{default}]' if default else ''}：")
    except EOFError:
        v = ""
    return v.strip() or default


def _yes(prompt: str, default: bool = True) -> bool:
    v = _ask(prompt + ("（Y/n）" if default else "（y/N）")).lower()
    return default if not v else v.startswith("y") or v in ("是", "要", "好")


async def add_interactive(cfg, roles_bg: tuple[str, ...], root: Path = ROOT) -> str | None:
    proxy = str(cfg.get("proxy") or "")
    print("\n粘贴接口地址（OpenAI 兼容，一般以 /v1 结尾；去你用的平台的文档里找“base_url”）：")
    url = _ask("接口地址")
    if not url:
        return None
    local = bool(re.search(r"127\.0\.0\.1|localhost", url))
    key = _ask("API Key（本机模型没有就直接回车）", secret=True)
    print("测连通、读模型列表……")
    models, url, via_proxy, err = await list_models(url, key, proxy)
    if err and not models:
        print(f"❌ 读不到模型列表：{err}")
        if "Key" in err and not local:
            return None
        name = _ask("这个接口不支持列模型的话，直接输入模型名（回车＝放弃）")
        if not name:
            return None
        models = [name]
    else:
        print(f"✅ 连上了：{url}" + ("（走代理）" if via_proxy else "") + f"，有 {len(models)} 个模型")
    shown = chat_models(models)
    if len(shown) > 40:
        kw = _ask(f"模型太多了（{len(shown)} 个），输入关键词筛一下（比如 qwen、deepseek、glm；回车＝全部列出）")
        if kw:
            shown = [m for m in shown if kw.lower() in m.lower()] or shown
    shown = shown or chat_models(models, show_all=True)
    for i, m in enumerate(shown[:80], 1):
        print(f"  {i}. {m}")
    pick = _ask("用哪个？输入编号或者模型名", "1" if len(shown) == 1 else "")
    model = shown[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= min(80, len(shown)) else pick
    if not model:
        return None
    print(f"试聊一句（{model}）……")
    ok, secs, text, extra = await probe_model(url, key, model, proxy, via_proxy)
    if not ok:
        print(f"❌ 试聊失败：{text}")
        if not _yes("还是要加上吗", False):
            return None
    else:
        print(f"✅ 回复（{secs:.1f} 秒）：{text}")
        if secs > 15:
            print("   有点慢：当主力的话回消息会比较慢，建议当备用或者干后台的活。")
    taken = set((cfg.get("llm") or {}).get("profiles") or {}) | set(
        ((load_custom(root).get("llm") or {}).get("profiles") or {}))
    name = _ask("给它起个名字（英文、数字、下划线）", profile_name(url, model, taken))
    print("\n怎么用它？")
    print("  1. 当主力：聊天优先用它（它限流 / 出错时自动换下一个）")
    print("  2. 当备用：别的都不行时才用它")
    print("  3. 只干后台的活（整理印象、排日程这些），聊天不用它")
    how = {"1": "first", "2": "last", "3": "background"}.get(_ask("选哪个", "1"), "first")
    name, routes = save_profile(cfg, url=url, key=key, model=model, name=name, how=how, roles_bg=roles_bg,
                                via_proxy=via_proxy, extra=extra, root=root)
    print(f"✅ 加好了：{name}（写在 {CUSTOM_FILE}，Key 在 .env）。重启程序后生效。")
    for r, names in routes.items():
        print(f"   {r}: {' → '.join(names)}")
    return name


async def test_all(cfg):
    from aigroup.llm.client import LLM
    llm = LLM(cfg)
    print(f"{'接口':<22}{'用时':>7}  回复")
    for name, p in llm.profiles.items():
        t0 = time.time()
        try:
            r = await p.client.chat.completions.create(
                model=p.model, messages=[{"role": "user", "content": "用一句话、十个字以内跟我打个招呼。"}],
                max_tokens=200, **({"extra_body": p.extra_body} if p.extra_body else {}))
            from aigroup.llm.client import strip_think
            print(f"{name:<22}{time.time() - t0:>6.1f}s  ✅ {strip_think(r.choices[0].message.content or '')[:40]}")
        except Exception as e:  # noqa: BLE001
            print(f"{name:<22}{time.time() - t0:>6.1f}s  ❌ {str(e)[:80]}")
    for r, names in llm.routes.items():
        print(f"  {r}: {' → '.join(names)}")


def remove_interactive(root: Path = ROOT) -> bool:
    profiles = ((load_custom(root).get("llm") or {}).get("profiles") or {})
    if not profiles:
        print("还没有自定义的接口。")
        return False
    names = list(profiles)
    for i, n in enumerate(names, 1):
        print(f"  {i}. {n}  {profiles[n].get('model')}  {profiles[n].get('base_url')}")
    v = _ask("删哪个？输入编号（回车＝不删）")
    if not (v.isdigit() and 1 <= int(v) <= len(names)):
        return False
    remove_profile(names[int(v) - 1], root)
    print(f"✅ 删掉了 {names[int(v) - 1]}（.env 里的 Key 没动）。重启程序后生效。")
    return True


def main(argv: list[str] | None = None, roles_bg: tuple[str, ...] = ("helper", "memory", "night")) -> int:
    """roles_bg：helper（其它小活）、memory（整理群友印象）、night（排她每天的安排）。"""
    from aigroup.core.config import load_config
    while True:
        cfg = load_config()
        custom = ((load_custom().get("llm") or {}).get("profiles") or {})
        print("\n=== 自定义大模型 ===")
        print("现在的接口：" + ("、".join(((cfg.get("llm") or {}).get("profiles") or {}).keys()) or "（还没有，先加一个）"))
        if custom:
            print("其中自己加的：" + "、".join(custom))
        print("  1. 加一个接口（填地址和 Key → 测连通 → 选模型 → 试聊）")
        print("  2. 把所有接口都试一遍")
        print("  3. 删掉一个自己加的接口")
        print("  回车. 退出")
        v = _ask("选哪个")
        if v == "1":
            asyncio.run(add_interactive(cfg, roles_bg))
        elif v == "2":
            asyncio.run(test_all(cfg))
        elif v == "3":
            remove_interactive()
        else:
            return 0
