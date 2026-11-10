# -*- coding: utf-8 -*-
"""把 PyInstaller 产物打成可直接上传 Release 的 zip。

用法:
    python tools/make_release_zip.py [产物目录] [输出目录]

默认产物目录 dist/桌宠大肥鱼，输出到桌面。
规则:
  - 顶层目录固定为 桌宠大肥鱼/
  - 文件名写 UTF-8 标志，别人解压不会乱码
  - 剔除 mp3（版权）、memory.db（个人记忆）、*.log（运行日志）
  - config.json 用 config.example.json 覆盖，保证 ai_api_key 为空
"""
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = "桌宠大肥鱼"
src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist", TOP)
out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.expanduser("~"), "Desktop")

if not os.path.isdir(src):
    print("[ERROR] 找不到打包产物:", src)
    sys.exit(1)

# 干净配置
example = os.path.join(ROOT, "config.example.json")
if os.path.isfile(example):
    shutil.copyfile(example, os.path.join(src, "config.json"))
else:
    print("[WARN] 没有 config.example.json，沿用现有 config.json（务必确认里面没有 API Key）")

stamp = time.strftime("%Y%m%d")
out = os.path.join(out_dir, "桌宠大肥鱼-windows-x64.zip")

# 同名旧包不覆盖，改名归档（保留历史版本，方便回滚）
if os.path.isfile(out):
    old = time.strftime("%Y%m%d", time.localtime(os.path.getmtime(out)))
    bak = os.path.join(out_dir, "桌宠大肥鱼-windows-x64-%s.zip" % old)
    n = 1
    while os.path.isfile(bak):
        bak = os.path.join(out_dir, "桌宠大肥鱼-windows-x64-%s-%d.zip" % (old, n))
        n += 1
    os.rename(out, bak)
    print("旧包已归档:", os.path.basename(bak))

count = 0
raw = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            arc = os.path.join(TOP, os.path.relpath(full, src)).replace(os.sep, "/")
            low = arc.lower()
            if low.endswith((".mp3", ".log", ".m4a", ".wav", ".flac")):
                continue
            if low.endswith("memory.db"):
                continue
            zf.write(full, arc)
            count += 1
            raw += os.path.getsize(full)

print("文件数 : %d" % count)
print("原始   : %.1f MB" % (raw / 1048576))
print("压缩后 : %.1f MB" % (os.path.getsize(out) / 1048576))
print("输出   :", out)
