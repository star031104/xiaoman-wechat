"""配置向导写出来的 config、单实例锁、配置检查。"""
import yaml

from aigroup.app import check, single_instance
from aigroup.wizard import build_config, pick_numbers


def test_build_config_roundtrip():
    old = {"proxy": "", "keys": {"groq": "${GROQ_KEY}"}, "wechat": {"group": {"interject_chance": 0.02},
                                                                    "send_key": "ctrl+enter"}}
    t = build_config(old, persona_file="persona/xiaoman.md", keys={"zhipu": "ZHIPU_KEY"}, owner="wxid_owner",
                     groups=["111@chatroom", "222@chatroom"], aliases=["满满"], owner_note="老朋友",
                     account="wxid_her_ab12", data_dir="")
    d = yaml.safe_load(t)
    assert d["keys"] == {"groq": "${GROQ_KEY}", "zhipu": "${ZHIPU_KEY}"}
    w = d["wechat"]
    assert w["groups"] == ["111@chatroom", "222@chatroom"] and w["owner"] == "wxid_owner"
    assert w["account"] == "wxid_her_ab12" and "data_dir" not in w
    assert w["send_key"] == "ctrl+enter"                                              # 原来手动改的保留
    assert w["group"] == {"interject_chance": 0.02, "owner_note": "老朋友"}
    assert pick_numbers("1, 3，9 x 3", 4) == [0, 2]


def test_borrow_llm_from_qq_version(tmp_path):
    """旁边装了 QQ 群版：它的 config.llm.yaml 和里面用到的 Key 拿过来（是用户自己配的接口，不是预置的）。"""
    from aigroup.core.secrets import read_env
    from aigroup.wizard import borrow_llm, sibling_qq
    qq, me = tmp_path / "ai-life-group", tmp_path / "ai-life-wechat"
    qq.mkdir()
    me.mkdir()
    assert sibling_qq(me) is None
    (qq / "config.llm.yaml").write_text("llm:\n  profiles:\n    ds: {base_url: 'https://x/v1', model: m, "
                                        "api_key: '${LLM_DS_API_KEY}'}\n", encoding="utf-8")
    (qq / ".env").write_text("LLM_DS_API_KEY=sk-test\nOTHER=1\n", encoding="utf-8")
    assert sibling_qq(me) == qq
    assert borrow_llm(qq, me) == 1
    assert (me / "config.llm.yaml").exists() and read_env(me) == {"LLM_DS_API_KEY": "sk-test"}


def test_check_and_lock(tmp_path, monkeypatch):
    from aigroup import app
    from aigroup.core.config import load_config
    monkeypatch.setattr(app.sys, "platform", "win32")
    c = load_config(tmp_path / "none.yaml")
    assert any("人设" in p for p in check(c))                                          # 还没写人设
    c["persona_file"] = "persona/template.md"
    assert any("groups" in p for p in check(c))
    c["wechat"]["groups"] = ["123"]
    assert any("groups" in p for p in check(c))                                       # 不是群 id
    c["wechat"]["groups"] = ["123@chatroom"]
    c["llm"] = {**c["llm"], "profiles": {}, "routes": {"chat": []}}     # 本机可能有自己加的 config.llm.yaml
    assert [p for p in check(c)] and "llm.bat" in check(c)[0]          # 仓库里不预置任何接口：要自己加
    c["llm"]["profiles"] = {"m": {"base_url": "https://x.example/v1", "model": "m", "api_key": "k"}}
    c["llm"]["routes"]["chat"] = ["m"]
    assert not check(c)
    monkeypatch.setattr(app.sys, "platform", "linux")
    assert not check(c)                                                 # Linux（服务器）也能跑
    monkeypatch.setattr(app.sys, "platform", "darwin")
    assert any("Windows" in p for p in check(c))
    a = single_instance(tmp_path / "run.lock")
    assert a is not None and single_instance(tmp_path / "run.lock") is None
    a.close()


def test_settings_in_sync():
    """设置只有一个“真”默认值：config.example.yaml 里 wechat.group 写的每一项都要在 chat/settings.py 的 DEFAULTS 里、值一样；
    小屋能调的 KNOBS 也都要有默认值。加设置时忘了同步，这个测试会提醒。"""
    import yaml

    from aigroup.chat.settings import DEFAULTS, KNOBS
    from aigroup.core.config import ROOT
    ex = yaml.safe_load((ROOT / "config.wechat.example.yaml").read_text(encoding="utf-8"))["wechat"]["group"]
    assert not [k for k in ex if k not in DEFAULTS], "config.example.yaml 里有 DEFAULTS 没有的设置"
    assert not {k: (ex[k], DEFAULTS[k]) for k in ex if ex[k] != DEFAULTS[k]}, "两边默认值不一样"
    assert not [k for k in KNOBS if k not in DEFAULTS], "KNOBS 里有没有默认值的开关"


def test_check_reports_bad_persona(tmp_path, monkeypatch):
    from aigroup import app
    from aigroup.core.config import load_config
    monkeypatch.setattr(app.sys, "platform", "win32")
    c = load_config(tmp_path / "none.yaml")
    c["wechat"]["groups"] = ["1@chatroom"]
    c["llm"] = {**c["llm"], "routes": None}                             # 写坏了也不崩
    c["persona_file"] = "persona/没有这个.md"
    probs = check(c)
    assert any("人设" in p for p in probs) and any("llm.bat" in p for p in probs)
