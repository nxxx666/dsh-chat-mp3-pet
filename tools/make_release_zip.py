# -*- coding: utf-8 -*-
"""把 PyInstaller 产物打成可直接上传 Release 的 zip。

用法:
    python tools/make_release_zip.py [产物目录] [输出目录] [版本号]

默认产物目录 dist/桌宠大肥鱼，输出到桌面。
版本号不填则自动递增：桌面上已有的 桌宠大肥鱼-windows-x64-<x.y.z>.zip
里取最大版本，补丁号 +1；一个都没有就从 0.0.0 开始。
产物名形如 桌宠大肥鱼-windows-x64-0.0.3.zip，同名绝不会覆盖。

规则:
  - 顶层目录固定为 桌宠大肥鱼/
  - 文件名写 UTF-8 标志，别人解压不会乱码
  - 剔除 mp3（版权）、memory.db（个人记忆）、*.log（运行日志）
  - config.json 用 config.example.json 覆盖，保证 ai_api_key 为空
"""
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = "桌宠大肥鱼"
src = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else os.path.join(ROOT, "dist", TOP)
out_dir = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else os.path.join(os.path.expanduser("~"), "Desktop")
force_ver = sys.argv[3].strip() if len(sys.argv) > 3 else ""

if not os.path.isdir(src):
    print("[ERROR] 找不到打包产物:", src)
    sys.exit(1)

VERSION_RE = re.compile(r"^%s-windows-x64-(\d+)\.(\d+)\.(\d+)\.zip$" % re.escape(TOP))


def scan_max_version():
    """桌面已有的版本号里取最大 (major, minor, patch)；没有则 None。"""
    best = None
    for name in os.listdir(out_dir):
        m = VERSION_RE.match(name)
        if not m:
            continue
        v = tuple(int(x) for x in m.groups())
        if best is None or v > best:
            best = v
    return best


def pick_version():
    if force_ver:
        m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", force_ver)
        if not m:
            print("[ERROR] 版本号格式应为 x.y.z，比如 0.0.3，收到:", force_ver)
            sys.exit(1)
        return tuple(int(x) for x in m.groups())

    best = scan_max_version()
    return (0, 0, 0) if best is None else (best[0], best[1], best[2] + 1)


ver = pick_version()

# 版本号被手动指定且撞名时，继续往后找空位，绝不覆盖旧包
while True:
    out = os.path.join(out_dir, "%s-windows-x64-%d.%d.%d.zip" % ((TOP,) + ver))
    if not os.path.isfile(out):
        break
    print("[WARN] 已存在同名包，版本号自动 +1:", os.path.basename(out))
    ver = (ver[0], ver[1], ver[2] + 1)

# 干净配置
example = os.path.join(ROOT, "config.example.json")
if os.path.isfile(example):
    shutil.copyfile(example, os.path.join(src, "config.json"))
else:
    print("[WARN] 没有 config.example.json，沿用现有 config.json（务必确认里面没有 API Key）")

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

print("版本   : %d.%d.%d" % ver)
print("文件数 : %d" % count)
print("原始   : %.1f MB" % (raw / 1048576))
print("压缩后 : %.1f MB" % (os.path.getsize(out) / 1048576))
print("输出   :", out)
