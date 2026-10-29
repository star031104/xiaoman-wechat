"""30.4 刚查过的简报只在“几乎同一个问题”时复用：换了比赛 / 项目要重新查。"""
from conftest import GROUP, _gc

LOL = ("判断 2026-10-30 凌晨（北京时间）的两场英雄联盟比赛谁能赢：先查清明天凌晨具体是哪两场比赛（赛事、对阵双方、开赛时间），"
       "再看两队近期战绩、选手状态、交手记录，给出胜负判断和理由")
CS = ("判断 2026-10-29 当天 PGL 赛事中 Vitality（小蜜蜂）对阵 NAVI 的比赛谁能赢：先查清具体是哪场比赛（CS2 还是别的项目、赛事名称、开赛时间），"
      "再看两队近期战绩、选手状态、地图池、交手记录，给出胜负判断和理由")
VAL = "2026 年 10 月 29 日（今天）无畏契约（VALORANT）晋升赛（Ascension）今天的两场比赛是哪两场、对阵双方、开赛时间，以及两队近期战绩和状态，判断谁能赢"
VAL_CN = "判断 今天无畏契约晋升赛两场比赛谁能赢：先查清是哪两场比赛，再看两队近期战绩、选手状态、交手记录，给出胜负判断和理由"
LOL_CN = "判断 明天凌晨两场英雄联盟比赛谁能赢：先查清是哪两场比赛，再看两队近期战绩、选手状态、交手记录，给出胜负判断和理由"


def test_other_match_not_reused(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._save_brief(GROUP, LOL, {"answer_points": ["KC 2-1 CFO"]})
    assert gc._similar_brief(GROUP, LOL) is not None
    assert gc._similar_brief(GROUP, CS) is None
    assert gc._similar_brief(GROUP, VAL) is None


def test_same_template_other_game_not_reused(cfg, brain):
    gc, _ = _gc(cfg, brain)
    gc._save_brief(GROUP, LOL_CN, {"answer_points": ["KC 2-1 CFO"]})
    assert gc._similar_brief(GROUP, VAL_CN) is None
    assert gc._similar_brief(GROUP, LOL_CN + "，说说理由") is not None
