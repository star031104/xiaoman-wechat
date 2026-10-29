"""31.1 押不存在的比赛：押的队名在查到的资料和群聊里都没出现就拦。"""
from conftest import FRIEND, GROUP, _gc


def test_bet_unknown(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._record(GROUP, FRIEND, "小王", "@你 后面三场你每把都必须选一只队伍", 1)
    assert "BLG" in gc._bet_unknown(GROUP, "押完，BLG、T1、GEN，就这么定")
    web = "# 你刚查了：10-30 凌晨 CFO 对 KC、BLG 对 TES"
    assert gc._bet_unknown(GROUP, "我押KC和BLG，KC 2-1 CFO", web) == ""
    assert "T1" in gc._bet_unknown(GROUP, "我押KC和T1", web)                  # 资料里没有 T1
    assert gc._bet_unknown(GROUP, "BLG这波团打得好", "") == ""                 # 不是在押
    assert gc._bet_unknown(GROUP, "我押2-1，把握不大") == ""                    # 没有队名
    assert gc._bet_unknown(GROUP, "押国羽，稳赢") == ""                        # 中文名不在这条规则管
    gc._record(GROUP, FRIEND, "小王", "待会小蜜蜂打NAVI谁能赢", 2)
    assert gc._bet_unknown(GROUP, "我看好navi") == ""                          # 群友刚说的（大小写不敏感）
