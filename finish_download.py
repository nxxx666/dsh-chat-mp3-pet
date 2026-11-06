"""下载收尾：补齐剩余分段并拼装。

对每个未完成的段，把「剩余范围」再拆成 8 条并行连接，
每条单独存 sub 文件，全部到位后按序合并成完整分段。
"""

import concurrent.futures as cf
import os
import sys
import time
import urllib.request

URL = "https://ollama.com/download/OllamaSetup.exe"
N_PARTS = 48
SUB_SPLIT = 8
OUT = os.path.join(os.path.expanduser("~"), "Desktop", "OllamaSetup.exe")
PART_DIR = os.path.join(os.path.dirname(OUT), ".ollama_parts")


def get_direct_url():
    req = urllib.request.Request(URL, method="HEAD")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())
    with opener.open(req, timeout=60) as resp:
        return resp.geturl(), int(resp.headers["Content-Length"])


def sub_download(url, path, start, end):
    """下载 [start,end] 到 path（不支持断点，失败抛异常，上层重试）。"""
    if os.path.isfile(path) and os.path.getsize(path) == end - start + 1:
        return
    req = urllib.request.Request(url)
    req.add_header("Range", f"bytes={start}-{end}")
    tmp = path + ".t"
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(tmp) != end - start + 1:
        raise IOError("sub 大小不符")
    os.replace(tmp, path)


def finish_part(i, url, p_start, p_end, retries=6):
    """把第 i 段补齐：已有字节保留在 .tmp，剩余拆 8 份并行。"""
    expect = p_end - p_start + 1
    part = os.path.join(PART_DIR, f"part{i:03d}")
    if os.path.isfile(part) and os.path.getsize(part) == expect:
        return f"段{i:03d} 已完成"

    # 现有字节（可能是完整段文件的一部分或 .tmp）
    have = 0
    if os.path.isfile(part):
        have = os.path.getsize(part)
    elif os.path.isfile(part + ".tmp"):
        have = os.path.getsize(part + ".tmp")

    rem_start = p_start + have
    rem = p_end - rem_start + 1

    if rem <= 0:
        os.replace(part + ".tmp", part)
        return f"段{i:03d} 补齐(重命名)"

    # 剩余范围拆 SUB_SPLIT 份
    per = rem // SUB_SPLIT
    subs = []
    for j in range(SUB_SPLIT):
        s = rem_start + j * per
        e = p_end if j == SUB_SPLIT - 1 else s + per - 1
        subs.append((j, s, e))

    for attempt in range(1, retries + 1):
        left = [(j, s, e) for j, s, e in subs
                if not (os.path.isfile(os.path.join(PART_DIR, f"part{i:03d}_{j}"))
                        and os.path.getsize(os.path.join(PART_DIR, f"part{i:03d}_{j}")) == e - s + 1)]
        if not left:
            break
        with cf.ThreadPoolExecutor(max_workers=len(left)) as ex:
            futs = {ex.submit(sub_download, url,
                              os.path.join(PART_DIR, f"part{i:03d}_{j}"), s, e): j
                    for j, s, e in left}
            bad = 0
            for fut in cf.as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    bad += 1
        if attempt < retries and bad:
            time.sleep(2)

    # 检查 sub 全部到位后合并
    with open(part + ".tmp" if have else part, "ab" if have else "wb") as f:
        pass
    # 统一处理：确保 base 部分在 part.tmp 里
    base = part + ".tmp"
    if os.path.isfile(part) and not os.path.isfile(base):
        os.rename(part, base)
    if not os.path.isfile(base):
        open(base, "wb").close()
    with open(base, "ab") as f:
        for j, _s, _e in subs:
            sp = os.path.join(PART_DIR, f"part{i:03d}_{j}")
            with open(sp, "rb") as sf:
                while True:
                    chunk = sf.read(1 << 22)
                    if not chunk:
                        break
                    f.write(chunk)
    if os.path.getsize(base) != expect:
        raise IOError(f"段{i:03d} 合并后大小不符: {os.path.getsize(base)} != {expect}")
    os.replace(base, part)
    # 清 sub
    for j, _s, _e in subs:
        for suf in ("", ".t"):
            try:
                os.remove(os.path.join(PART_DIR, f"part{i:03d}_{j}{suf}"))
            except OSError:
                pass
    return f"段{i:03d} 补齐完成"


def main():
    os.makedirs(PART_DIR, exist_ok=True)
    print("解析直链…", flush=True)
    url, total = get_direct_url()
    per = total // N_PARTS
    bounds = [(i, i * per, (total - 1) if i == N_PARTS - 1 else i * per + per - 1)
              for i in range(N_PARTS)]

    todo = []
    for i, s, e in bounds:
        part = os.path.join(PART_DIR, f"part{i:03d}")
        if os.path.isfile(part) and os.path.getsize(part) == e - s + 1:
            continue
        todo.append((i, s, e))
    print(f"待补 {len(todo)} 段 / 共 {N_PARTS} 段", flush=True)

    with cf.ThreadPoolExecutor(max_workers=len(todo)) as ex:
        futs = {ex.submit(finish_part, i, url, s, e): i for i, s, e in todo}
        for fut in cf.as_completed(futs):
            try:
                print(" ", fut.result(), flush=True)
            except Exception as exc:
                print(f"  失败（重跑本脚本续传）：{exc}", flush=True)

    # 最终检查 + 拼装
    missing = [i for i, s, e in bounds
               if not (os.path.isfile(os.path.join(PART_DIR, f"part{i:03d}"))
                       and os.path.getsize(os.path.join(PART_DIR, f"part{i:03d}")) == e - s + 1)]
    if missing:
        print(f"还缺 {len(missing)} 段：{missing}，请重跑", flush=True)
        return 1

    print("全部段就绪，拼装…", flush=True)
    with open(OUT + ".tmp", "wb") as out:
        for i in range(N_PARTS):
            with open(os.path.join(PART_DIR, f"part{i:03d}"), "rb") as f:
                while True:
                    chunk = f.read(1 << 24)
                    if not chunk:
                        break
                    out.write(chunk)
    os.replace(OUT + ".tmp", OUT)
    final = os.path.getsize(OUT)
    ok = final == total
    print(f"完成 {OUT}\n大小 {final:,} / 期望 {total:,} -> "
          + ("校验通过" if ok else "不符！"), flush=True)
    if ok:
        import shutil
        shutil.rmtree(PART_DIR, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
