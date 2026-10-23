"""13.0 彻底忘掉一个人 / 一个群（python run.py forget-person 编号 / forget-group 群 id）。

删的东西：聊天记录、画像（含历史版本）、事实（关于他的、他说的）、门控日志、回放、学到的说法和黑话、群梗、
她查过记下的东西、约好的事、熟悉度、所在城市、kv 里按群 / 按人存的东西；最后 VACUUM，删掉的内容不会留在数据库文件里。
备份（data/backup/）不动：要连备份一起清，删掉那几个备份文件就行（程序会提示）。
"""
from __future__ import annotations

import re


def _tables(db) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def forget_person(db, uid: int) -> dict:
    uid = int(uid)
    t = _tables(db)
    n: dict[str, int] = {}

    def run(name, sql, args):
        if name in t:
            n[name] = n.get(name, 0) + db.conn.execute(sql, args).rowcount

    mids = [r[0] for r in db.conn.execute("SELECT id FROM qq_group_messages WHERE user_id=?", (uid,))] \
        if "qq_group_messages" in t else []
    if "qq_embed" in t:
        for i in range(0, len(mids), 500):
            chunk = mids[i:i + 500]
            run("qq_embed", f"DELETE FROM qq_embed WHERE msg_id IN ({','.join('?' * len(chunk))})", chunk)
    run("qq_group_messages", "DELETE FROM qq_group_messages WHERE user_id=?", (uid,))
    for tab in ("qq_people", "qq_profiles", "qq_profile_versions", "qq_closeness", "qq_where"):
        run(tab, f"DELETE FROM {tab} WHERE user_id=?", (uid,))
    run("qq_facts", "DELETE FROM qq_facts WHERE subject_id=? OR speaker_id=?", (uid, uid))
    run("qq_plans", "DELETE FROM qq_plans WHERE user_id=?", (uid,))
    run("qq_reply_effect", "UPDATE qq_reply_effect SET target_uid=NULL WHERE target_uid=?", (uid,))
    k = 0
    for (key,) in db.conn.execute("SELECT k FROM kv").fetchall():
        if re.search(rf"(^|_){uid}($|_)", key):
            db.conn.execute("DELETE FROM kv WHERE k=?", (key,))
            k += 1
    n["kv"] = k
    db.conn.commit()
    db.conn.execute("VACUUM")
    return n


def forget_group(db, gid: int) -> dict:
    gid = int(gid)
    t = _tables(db)
    n: dict[str, int] = {}
    if "qq_embed" in t and "qq_group_messages" in t:
        n["qq_embed"] = db.conn.execute("DELETE FROM qq_embed WHERE msg_id IN (SELECT id FROM qq_group_messages "
                                        "WHERE group_id=?)", (gid,)).rowcount
    for tab in ("qq_group_messages", "qq_profiles", "qq_profile_versions", "qq_reply_effect", "qq_gate_log", "qq_trace",
                "qq_expressions", "qq_jargon", "qq_facts", "qq_lore", "qq_knowledge", "qq_plans"):
        if tab in t:
            n[tab] = db.conn.execute(f"DELETE FROM {tab} WHERE group_id=?", (gid,)).rowcount
    if "qq_people" in t and "qq_profiles" in t:          # 只在这个群出现过的人：人也删掉
        n["qq_people"] = db.conn.execute("DELETE FROM qq_people WHERE user_id NOT IN (SELECT user_id FROM qq_profiles) "
                                         "AND user_id NOT IN (SELECT user_id FROM qq_group_messages)").rowcount
    k = 0
    for (key,) in db.conn.execute("SELECT k FROM kv").fetchall():
        if re.search(rf"_g?{gid}($|_)", key):
            db.conn.execute("DELETE FROM kv WHERE k=?", (key,))
            k += 1
    n["kv"] = k
    db.conn.commit()
    db.conn.execute("VACUUM")
    return n
