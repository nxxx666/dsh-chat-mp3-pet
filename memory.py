"""长期记忆库（SQLite + FTS5）。

解决两个问题：
1. 上下文条数固定、聊久了早期内容被硬截断 → 现在截断前先「摘要压缩」存进来
2. 隔天再聊就什么都不记得 → 每句话都落库，提问时按相关度把旧片段捞回来

检索用「混合打分」（借鉴 DeepSeek Harness 的召回思路）：
    score = 0.55 * 词法覆盖 + 0.30 * 时间衰减 + 0.15 * 重要度
- 词法覆盖：查询切成的二字词，命中比例（中文用 bigram 索引，不需要分词器）
- 时间衰减：越近越值钱，半衰期 30 天
- 重要度：用户自述（我叫…/我喜欢…/记住…）和长句权重更高

设计约束：
- 数据库文件放在 exe 旁边（memory.db），删掉就等于忘记一切
- 任何异常都不许把桌宠搞崩：初始化失败就退化成「没有记忆」的空壳
"""

import os
import re
import sqlite3
import threading
import time

import paths

DB_NAME = "memory.db"

# ---- 混合打分的权重 ----
W_LEXICAL = 0.55
W_RECENCY = 0.30
W_IMPORTANCE = 0.15
HALF_LIFE_DAYS = 30.0      # 时间衰减半衰期
MIN_CONTENT_LEN = 4        # 太短的「嗯」「好」不进库
MIN_COVERAGE = 0.10        # 词法覆盖低于此值视为不相关（宁缺勿错）

# 单字索引里剔掉这些高频虚词，否则「我/的/在」会把所有句子都算成相关
STOP_CHARS = set("的了是我你他她它在和也就都吗呢吧啊么这那个有要想很太")

# 用户自述类关键词：这类话对「记住这个人」最有价值
_SELF_WORDS = ("我叫", "我是", "我喜欢", "我不喜欢", "我讨厌", "我的", "记住",
               "别忘", "记得", "以后", "每次", "习惯", "生日", "住在", "在做")

RE_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
RE_WORD = re.compile(r"[a-z0-9_]+")

W_UNIGRAM = 1              # 单字：只能证明「沾边」
W_BIGRAM = 3               # 二字词：真命中
W_WORD = 4                 # 英文数字整词


def weighted_tokens(text: str):
    """切成 [(token, 权重)]。

    中文同时建「单字」和「二字滑窗」两层：
    - 只有 bigram 的话，「我的猫叫什么」匹配不到「橘猫」（没有共同的二字词）
    - 只有单字的话，虚词太多、噪音大
    两层结合 + 权重区分，既能召回又能排序。

    不用 jieba 之类分词器：bigram 不需要词表，短句场景召回率更高。
    """
    s = (text or "").lower()
    pairs = []

    for m in RE_CJK.finditer(s):
        run = m.group(0)
        for ch in run:
            if ch not in STOP_CHARS:
                pairs.append((ch, W_UNIGRAM))
        for i in range(len(run) - 1):
            pairs.append((run[i:i + 2], W_BIGRAM))

    for w in RE_WORD.findall(s):
        pairs.append((w, W_WORD))

    # 同一 token 只留最大权重，且保序（FTS 查询串更好读）
    best, order = {}, []
    for tok, w in pairs:
        if tok not in best:
            best[tok] = w
            order.append(tok)
        elif w > best[tok]:
            best[tok] = w
    return [(t, best[t]) for t in order]


def tokens_of(text: str):
    """只要 token 列表（给 FTS 查询和调试用）。"""
    return [t for t, _ in weighted_tokens(text)]


def _importance_of(role: str, content: str) -> float:
    """0~1：多长、谁说的、是不是在讲自己。"""
    val = 0.30 if role == "assistant" else 0.40
    val += min(0.35, len(content) / 300.0 * 0.35)      # 长句信息多
    if role == "user" and any(w in content for w in _SELF_WORDS):
        val += 0.20                                    # 讲自己的事优先记住
    if role == "summary":
        val = 0.95
    return round(max(0.0, min(1.0, val)), 3)


def _fmt_time(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


class MemoryStore:
    """线程安全的记忆库。主线程写、推理线程读，所以统一加锁。"""

    def __init__(self, path: str = None):
        self.path = path or os.path.join(paths.base_dir(), DB_NAME)
        self.ok = False
        self.error = ""          # 降级原因，给人看的（菜单/日志里能查为什么没记忆）
        self._lock = threading.Lock()
        self._db = None
        try:
            self._db = sqlite3.connect(self.path, check_same_thread=False,
                                       timeout=3.0)
            self._db.row_factory = sqlite3.Row
            self._create()
            self.ok = True
        except (sqlite3.Error, OSError) as exc:
            self.error = repr(exc)
            print("[memory] 记忆库不可用，本次运行不带长期记忆：%r" % (exc,))

    # ---------- 建表 ----------
    def _create(self):
        with self._lock:
            c = self._db.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS memos(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                idx TEXT NOT NULL,
                persona TEXT NOT NULL DEFAULT '',
                session TEXT NOT NULL DEFAULT '',
                importance REAL NOT NULL DEFAULT 0.5,
                is_summary INTEGER NOT NULL DEFAULT 0
            )""")
            c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memos_fts
                         USING fts5(idx, tokenize='unicode61')""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memos_ts ON memos(ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memos_sum "
                      "ON memos(is_summary, persona, ts)")
            self._db.commit()

    # ---------- 写 ----------
    def add(self, role: str, content: str, persona: str = "",
            session: str = "", importance: float = None) -> int:
        """存一条。返回 id（失败或太短返回 0）。"""
        content = (content or "").strip()
        if not self.ok or len(content) < MIN_CONTENT_LEN:
            return 0
        idx = " ".join(tokens_of(content))
        imp = _importance_of(role, content) if importance is None else importance
        summary_flag = 1 if role == "summary" else 0
        try:
            with self._lock:
                c = self._db.cursor()
                c.execute(
                    "INSERT INTO memos(ts, role, content, idx, persona, session,"
                    " importance, is_summary) VALUES(?,?,?,?,?,?,?,?)",
                    (time.time(), role, content, idx, persona, session, imp,
                     summary_flag))
                mid = c.lastrowid
                c.execute("INSERT INTO memos_fts(rowid, idx) VALUES(?,?)",
                          (mid, idx))
                self._db.commit()
            return mid
        except sqlite3.Error as exc:
            print("[memory] 写入失败：%r" % (exc,))
            return 0

    def add_summary(self, content: str, persona: str = "") -> int:
        return self.add("summary", content, persona=persona, importance=0.95)

    # ---------- 读 ----------
    def latest_summary(self, persona: str = "") -> str:
        """当前人设最近一次的长期摘要（没有就是空串）。"""
        if not self.ok:
            return ""
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT content FROM memos WHERE is_summary=1 AND persona=?"
                    " ORDER BY ts DESC LIMIT 1", (persona or "",)).fetchone()
            return row["content"] if row else ""
        except sqlite3.Error:
            return ""

    def search(self, query: str, k: int = 3, persona: str = "",
               exclude_ids=(), half_life_days: float = HALF_LIFE_DAYS):
        """按混合打分召回最相关的 k 条。返回 [dict(id, role, content, ts, score, ...)]"""
        if not self.ok or not (query or "").strip() or k <= 0:
            return []
        q_tokens = weighted_tokens(query)
        if not q_tokens:
            return []

        rows = self._fts_rows([t for t, _ in q_tokens], persona,
                              limit=max(40, k * 8))
        if not rows:
            rows = self._like_rows(q_tokens, persona, limit=max(40, k * 8))

        now = time.time()
        exclude = set(exclude_ids or ())
        total_w = sum(w for _, w in q_tokens)
        scored = []
        for r in rows:
            if r["id"] in exclude:
                continue
            idx_tokens = set((r["idx"] or "").split())
            hit_w = sum(w for t, w in q_tokens if t in idx_tokens)
            coverage = hit_w / total_w if total_w else 0.0
            if coverage < MIN_COVERAGE:
                continue
            age_days = max(0.0, (now - r["ts"]) / 86400.0)
            recency = 0.5 ** (age_days / half_life_days)
            score = (W_LEXICAL * coverage
                     + W_RECENCY * recency
                     + W_IMPORTANCE * (r["importance"] or 0.5))
            scored.append({
                "id": r["id"], "role": r["role"], "content": r["content"],
                "ts": r["ts"], "time": _fmt_time(r["ts"]),
                "score": round(score, 4), "coverage": round(coverage, 3),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return self._dedupe(scored)[:k]

    def _fts_rows(self, tokens, persona, limit):
        expr = " OR ".join(f'"{t}"' for t in tokens[:40])
        # is_summary=0：摘要已经整段注入 system 了，再当「旧片段」召回就是重复
        sql = ("SELECT m.* FROM memos_fts f JOIN memos m ON m.id=f.rowid "
               "WHERE memos_fts MATCH ? AND m.is_summary=0")
        args = [expr]
        if persona:
            sql += " AND m.persona=?"
            args.append(persona)
        sql += " ORDER BY bm25(memos_fts) LIMIT ?"
        args.append(limit)
        try:
            with self._lock:
                return self._db.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            print("[memory] 检索失败：%r" % (exc,))
            return []

    def _like_rows(self, q_tokens, persona, limit):
        """FTS 一条都没命中时走模糊匹配兜底（比如超短查询）。"""
        needle = q_tokens[0][0] if q_tokens else ""
        if not needle:
            return []
        sql = "SELECT * FROM memos WHERE content LIKE ? AND is_summary=0"
        args = [f"%{needle}%"]
        if persona:
            sql += " AND persona=?"
            args.append(persona)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        try:
            with self._lock:
                return self._db.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []

    @staticmethod
    def _dedupe(items, prefix: int = 24):
        """内容高度重复的只留分高的那条。"""
        seen, out = set(), []
        for it in items:
            key = it["content"][:prefix]
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    def recent(self, n: int = 20, persona: str = None):
        if not self.ok:
            return []
        sql = "SELECT * FROM memos"
        args = []
        if persona is not None:
            sql += " WHERE persona=?"
            args.append(persona)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, n))
        try:
            with self._lock:
                rows = self._db.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        return [{"id": r["id"], "role": r["role"], "content": r["content"],
                 "ts": r["ts"], "time": _fmt_time(r["ts"])} for r in rows]

    def count(self, persona: str = None) -> int:
        if not self.ok:
            return 0
        sql, args = "SELECT COUNT(*) AS n FROM memos", []
        if persona is not None:
            sql += " WHERE persona=?"
            args.append(persona)
        try:
            with self._lock:
                return int(self._db.execute(sql, args).fetchone()["n"])
        except sqlite3.Error:
            return 0

    def sessions(self, persona: str = None) -> int:
        """聊过几「场」——按 session 去重，用来在菜单里显示个大概。"""
        if not self.ok:
            return 0
        sql = "SELECT COUNT(DISTINCT session) AS n FROM memos WHERE session<>''"
        args = []
        if persona is not None:
            sql += " AND persona=?"
            args.append(persona)
        try:
            with self._lock:
                return int(self._db.execute(sql, args).fetchone()["n"])
        except sqlite3.Error:
            return 0

    # ---------- 维护 ----------
    def clear(self, persona: str = None) -> int:
        """清空记忆（可按人设清）。返回删掉多少条。"""
        if not self.ok:
            return 0
        try:
            with self._lock:
                c = self._db.cursor()
                total = c.execute("SELECT COUNT(*) FROM memos").fetchone()[0]
                if persona is None:
                    c.execute("DELETE FROM memos")
                    c.execute("DELETE FROM memos_fts")
                    n = total
                else:
                    ids = [r[0] for r in c.execute(
                        "SELECT id FROM memos WHERE persona=?", (persona,))]
                    for mid in ids:
                        c.execute("DELETE FROM memos WHERE id=?", (mid,))
                        c.execute("DELETE FROM memos_fts WHERE rowid=?", (mid,))
                    n = len(ids)
                self._db.commit()
            return max(0, n)
        except sqlite3.Error as exc:
            print("[memory] 清空失败：%r" % (exc,))
            return 0

    def vacuum(self):
        if not self.ok:
            return
        try:
            with self._lock:
                self._db.execute("VACUUM")
        except sqlite3.Error:
            pass

    def close(self):
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None
            self.ok = False


# ============================================================
# 提示词拼装（给 pet_window 用，放这里好单独测）
# ============================================================
RECALL_HEADER = "【想起一些旧事】"
RECALL_FOOTER = "（这些是很久以前的对话碎片，只在确实相关时自然提一句，不要整段复述，也不要说「根据我的记忆」。）"


def format_recall(hits) -> str:
    """把召回结果拼成注入 system 的一段文字。"""
    if not hits:
        return ""
    lines = []
    for h in hits:
        who = "用户" if h["role"] == "user" else "你"
        text = h["content"].replace("\n", " ").strip()
        if len(text) > 90:
            text = text[:90] + "…"
        lines.append(f"- {h['time']}｜{who}：{text}")
    return "\n".join([RECALL_HEADER, *lines, RECALL_FOOTER])


def summary_prompt(prev: str, lines) -> list:
    """让模型把「旧摘要 + 新丢弃的对话」压成一段不超过 200 字的记忆。"""
    body = "\n".join(lines) if lines else "（无）"
    sys_msg = (
        "你是对话记忆整理器。把下面的聊天记录压缩成一段长期记忆，"
        "要求：\n"
        "1. 只留「关于用户的事实、偏好、约定、正在做的事」，删掉寒暄和语气词；\n"
        "2. 用第三人称陈述句，如「用户在做一个桌宠项目，喜欢简洁的回复」；\n"
        "3. 200 字以内，不要分点，不要 Markdown，不要复述原句；\n"
        "4. 若与旧记忆重复就合并，不要写「用户又问了一遍」。"
    )
    user_msg = f"【旧记忆】\n{prev or '（还没有）'}\n\n【新的聊天记录】\n{body}"
    return [{"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg}]


# ---------- 命令行小工具：看看库里有什么 ----------
if __name__ == "__main__":
    import sys
    store = MemoryStore()
    print("库文件：", store.path, "| 可用：", store.ok)
    print("总条数：", store.count(), "| 聊过", store.sessions(), "场")
    if len(sys.argv) > 1:
        hits = store.search(" ".join(sys.argv[1:]), k=8)
        print(f"\n搜索「{' '.join(sys.argv[1:])}」→ {len(hits)} 条")
        for h in hits:
            print(f"  {h['score']:.3f} [{h['role']}] {h['time']} {h['content'][:60]}")
    else:
        for r in store.recent(10):
            print(f"  [{r['role']}] {r['time']} {r['content'][:60]}")
