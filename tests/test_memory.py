# -*- coding: utf-8 -*-
"""记忆库单元测试（不碰 UI、不需要 API Key）。

跑： PYTHONIOENCODING=utf-8 /d/Anaconda/python.exe test_memory.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.abspath("."))

import memory   # noqa: E402

DB = os.path.abspath("_test_memory.db")
ok_all = True
_seq = 0


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + str(extra)) if extra else ''}")


def fresh_store():
    """每个用例一个独立库文件；不清文件（删文件会被安全策略拦），改用 clear()。"""
    global _seq
    _seq += 1
    s = memory.MemoryStore(os.path.abspath(f"_test_memory_{_seq}.db"))
    assert s.ok, "测试库建不起来"
    s.clear()
    return s


# ---------- 1. token 化 ----------
toks = memory.tokens_of("今天天气很好")
check("中文切出二字词", {"今天", "天气", "很好"} <= set(toks), toks)
check("中文也切出单字（用来兜住变体说法）", {"今", "天", "气", "好"} <= set(toks), toks)
check("虚词不进单字索引", "的" not in memory.tokens_of("我的猫"), memory.tokens_of("我的猫"))
check("二字词权重高于单字",
      dict(memory.weighted_tokens("桌宠"))["桌宠"] >
      dict(memory.weighted_tokens("桌宠"))["桌"], memory.weighted_tokens("桌宠"))
check("英文按整词", "python" in memory.tokens_of("我在学 Python 和 SQLite"))
check("标点不进索引", all("，" not in t for t in memory.tokens_of("你好，世界")))
check("空串返回空", memory.tokens_of("") == [])

# ---------- 2. 基本召回 ----------
s = fresh_store()
s.add("user", "我养了一只叫团子的橘猫，特别能吃", persona="大肥鱼", session="s1")
s.add("user", "最近在做一个桌宠项目，用的是 PyQt6", persona="大肥鱼", session="s1")
s.add("assistant", "好的呀，那你要记得按时吃饭哦", persona="大肥鱼", session="s1")
s.add("user", "明天要去看牙医，有点紧张", persona="大肥鱼", session="s2")

check("写入成功", s.count("大肥鱼") == 4, s.count("大肥鱼"))

hits = s.search("我的猫叫什么", k=2, persona="大肥鱼")
check("问猫能召回猫那条", hits and "橘猫" in hits[0]["content"],
      hits[0]["content"][:30] if hits else "无结果")
check("召回带分数和角色", hits and {"id", "score", "role", "ts"} <= set(hits[0]))

hits2 = s.search("桌宠用什么写的", k=2, persona="大肥鱼")
check("问桌宠召回桌宠那条", hits2 and "PyQt6" in hits2[0]["content"],
      hits2[0]["content"][:30] if hits2 else "无结果")

# ---------- 3. 无关查询不该乱召回 ----------
nonsense = s.search("量子纠缠和黑洞蒸发", k=3, persona="大肥鱼")
check("完全无关的问题不硬凑（宁缺勿错）", len(nonsense) <= 1,
      [h["content"][:16] for h in nonsense])

# ---------- 4. 时间衰减 ----------
s2 = fresh_store()
old_id = s2.add("user", "我喜欢喝冰美式咖啡，每天早上都要来一杯", persona="p")
new_id = s2.add("user", "我喜欢喝冰美式咖啡", persona="p")
# 把第一条的时间往前挪 120 天（4 个半衰期），模拟「很久以前说过」
with s2._lock:
    s2._db.execute("UPDATE memos SET ts=? WHERE id=?",
                   (time.time() - 120 * 86400, old_id))
    s2._db.commit()
hits = s2.search("冰美式", k=2, persona="p")
check("同等相关时新的排在前面", hits and hits[0]["id"] == new_id,
      [(h["id"], h["score"]) for h in hits])
check("旧的那条分数更低", len(hits) == 2 and hits[1]["score"] < hits[0]["score"],
      [(h["id"], h["score"]) for h in hits])

# ---------- 5. 重要度 ----------
s3 = fresh_store()
s3.add("assistant", "嗯嗯好的呢", persona="p")
s3.add("user", "记住：我的生日是 3 月 14 日，别忘记", persona="p")
hi = s3.search("生日", k=1, persona="p")
check("用户自述权重更高（长句+自述词）", hi and hi[0]["content"].startswith("记住"),
      hi[0]["content"][:20] if hi else "无")

# ---------- 6. 单字查询兜底 ----------
one = s3.search("猫", k=3, persona="p")
check("单字查询不报错（走 LIKE 兜底）", isinstance(one, list))

# ---------- 7. 摘要 ----------
s.add_summary("用户养了一只叫团子的橘猫，正在做一个 PyQt6 桌宠项目。", persona="大肥鱼")
check("摘要能读回", "团子" in s.latest_summary("大肥鱼"))
s.add_summary("用户又加了一句：他喜欢简洁的回复。", persona="大肥鱼")
check("摘要取最新一条", "简洁" in s.latest_summary("大肥鱼"),
      s.latest_summary("大肥鱼"))
check("摘要也算进总数（4 条对话 + 2 条摘要）", s.count("大肥鱼") == 6, s.count("大肥鱼"))
check("摘要不参与召回（它已经整段在 system 里了）",
      all(h["role"] != "summary" for h in s.search("团子 桌宠", k=6, persona="大肥鱼")),
      [h["role"] for h in s.search("团子 桌宠", k=6, persona="大肥鱼")])

# ---------- 8. 人设隔离 ----------
s.add("user", "我偏爱冷色调的界面", persona="小蓝")
check("按人设检索互不串味",
      all(h["content"] != "我偏爱冷色调的界面"
          for h in s.search("冷色调界面", k=5, persona="大肥鱼")))
check("另一个人设能查到自己的",
      any("冷色调" in h["content"] for h in s.search("冷色调界面", k=5, persona="小蓝")))

# ---------- 9. exclude_ids ----------
first = s.search("团子", k=3, persona="大肥鱼")
if first:
    again = s.search("团子", k=3, persona="大肥鱼", exclude_ids=[first[0]["id"]])
    check("exclude_ids 能排掉自己刚说的话",
          all(h["id"] != first[0]["id"] for h in again),
          [h["id"] for h in again])

# ---------- 10. 提示词拼装 ----------
block = memory.format_recall(hits if hits else [
    {"role": "user", "content": "我喜欢冰美式", "time": "01-02 03:04"}])
check("召回块有表头表尾", memory.RECALL_HEADER in block and
      "不要整段复述" in block, block[:40])
check("空召回返回空串", memory.format_recall([]) == "")

msgs = memory.summary_prompt("旧记忆内容", ["用户：你好", "你：嗯嗯"])
check("摘要 prompt 是 system+user 两条", len(msgs) == 2 and
      msgs[0]["role"] == "system" and msgs[1]["role"] == "user")
check("摘要 prompt 带上了旧记忆与新记录",
      "旧记忆内容" in msgs[1]["content"] and "你好" in msgs[1]["content"])

# ---------- 11. 清空 ----------
n = s.clear("小蓝")
check("按人设清空只清自己的", n >= 1 and s.count("小蓝") == 0 and s.count("大肥鱼") > 0,
      f"删了 {n} 条")
n = s.clear()
check("全清后为空", s.count() == 0, s.count())

# ---------- 12. 降级不崩 ----------
bad = memory.MemoryStore(os.path.join("不存在的盘符Z:", "x", "m.db"))
check("路径不可用时 ok=False", bad.ok is False)
check("降级后 add 不抛且返回 0", bad.add("user", "随便说点什么") == 0)
check("降级后 search 返回空", bad.search("随便") == [])
check("降级后 count 为 0", bad.count() == 0)
check("降级后 latest_summary 为空", bad.latest_summary("x") == "")

# ---------- 13. 太短的不入库 ----------
s4 = fresh_store()
s4.add("user", "嗯")
s4.add("user", "好")
check("寒暄短句不入库", s4.count() == 0, s4.count())

s.close()
s2.close()
s3.close()
s4.close()
bad.close()
for f in os.listdir("."):
    if f.startswith("_test_memory") and f.endswith(".db"):
        try:
            os.remove(f)      # 测试产物，清掉；被安全策略拦就算了
        except OSError:
            pass
if os.path.isfile(DB):
    try:
        os.remove(DB)
    except OSError:
        pass

print("\nRESULT:", "ALL PASS" if ok_all else "HAS FAILURE")
sys.exit(0 if ok_all else 1)
