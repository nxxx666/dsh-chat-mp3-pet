"""并行分段下载 Ollama 安装包（带字节级续传）。

特点：
- 48 路并发（瓶颈是每连接限速，不是总带宽）
- 每段支持字节级续传：进程被杀也不丢已下载字节
- 跑完所有段自动拼装 + 大小校验
- 幂等：重复运行会自动跳过已完成部分
"""

import concurrent.futures as cf
import os
import sys
import time
import urllib.request

URL = "https://ollama.com/download/OllamaSetup.exe"
N_PARTS = 48
RUN_SECONDS = 480          # 单次最多跑 8 分钟，留余量给外层循环重启
OUT = os.path.join(os.path.expanduser("~"), "Desktop", "OllamaSetup.exe")
PART_DIR = os.path.join(os.path.dirname(OUT), ".ollama_parts")


def get_direct_url():
    req = urllib.request.Request(URL, method="HEAD")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())
    with opener.open(req, timeout=60) as resp:
        return resp.geturl(), int(resp.headers["Content-Length"])


def download_part(i, url, start, end):
    path = os.path.join(PART_DIR, f"part{i:03d}")
    expect = end - start + 1
    if os.path.isfile(path) and os.path.getsize(path) == expect:
        return i, expect, True

    tmp = path + ".tmp"
    have = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
    if have > expect:
        os.remove(tmp)
        have = 0

    req = urllib.request.Request(url)
    req.add_header("Range", f"bytes={start + have}-{end}")
    with urllib.request.urlopen(req, timeout=90) as resp, open(tmp, "ab") as f:
        while True:
            chunk = resp.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(tmp) != expect:
        raise IOError(f"part{i} 大小不符")
    os.replace(tmp, path)
    return i, expect, False


def assemble(total):
    print("拼装…")
    with open(OUT + ".tmp", "wb") as out:
        for i in range(N_PARTS):
            with open(os.path.join(PART_DIR, f"part{i:03d}"), "rb") as f:
                while True:
                    chunk = f.read(1 << 24)
                    if not chunk:
                        break
                    out.write(chunk)
    os.replace(OUT + ".tmp", OUT)
    ok = os.path.getsize(OUT) == total
    if ok:
        for i in range(N_PARTS):
            try:
                os.remove(os.path.join(PART_DIR, f"part{i:03d}"))
                os.remove(os.path.join(PART_DIR, f"part{i:03d}.tmp"))
            except OSError:
                pass
        try:
            os.rmdir(PART_DIR)
        except OSError:
            pass
    return ok


def main():
    os.makedirs(PART_DIR, exist_ok=True)
    print("解析直链…", flush=True)
    url, total = get_direct_url()
    per = total // N_PARTS
    bounds = [(i, i * per, (total - 1) if i == N_PARTS - 1 else i * per + per - 1)
              for i in range(N_PARTS)]

    t0 = time.time()
    print(f"总 {total/1e9:.2f} GB，{N_PARTS} 段，本次最多 {RUN_SECONDS}s", flush=True)

    with cf.ThreadPoolExecutor(max_workers=N_PARTS) as ex:
        futs = {ex.submit(download_part, i, url, s, e): i for i, s, e in bounds}
        done = 0
        for fut in cf.as_completed(futs):
            try:
                i, size, cached = fut.result()
            except Exception as exc:
                print(f"  某段出错（下次续传）：{exc}", flush=True)
                continue
            done += 1
            mark = "缓存" if cached else "完成"
            print(f"  段{i:03d} {mark} 累计 {done}/{N_PARTS}", flush=True)
            if time.time() - t0 > RUN_SECONDS:
                print("到时间上限，退出等待重启续传", flush=True)
                return 2

    ok = assemble(total)
    print(f"\n大小校验{'通过' if ok else '失败'}：{OUT}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
