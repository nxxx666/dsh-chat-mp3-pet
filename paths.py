"""路径工具：同时兼容「源码运行」和「PyInstaller 打包后运行」。

打包后：
- resource_dir() 指向 exe 内部的临时解压目录（只读资源，如 assets/pet.png）
- base_dir()   指向 exe 所在的文件夹（可写，放人设、音乐、配置）

这样用户依然可以直接编辑人设文件、往 music/ 里放歌。
"""

import os
import shutil
import sys


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_dir() -> str:
    """只读资源目录（打包后是 _MEIPASS，源码运行时是脚本所在目录）。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def base_dir() -> str:
    """可写目录：人设、音乐、配置文件都放这里。"""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def ensure_external(subdir: str, seed_from_resource: bool = True) -> str:
    """确保 exe 旁边存在 subdir 目录（没有就建）；可选从包内复制默认内容。"""
    target = os.path.join(base_dir(), subdir)
    created = False
    if not os.path.isdir(target):
        try:
            os.makedirs(target, exist_ok=True)
            created = True
        except OSError:
            return target

    # 首次创建时，把包内自带的内容（如默认人设）复制出来，方便用户直接改
    if created and seed_from_resource:
        src = os.path.join(resource_dir(), subdir)
        if os.path.isdir(src):
            try:
                shutil.copytree(src, target, dirs_exist_ok=True)
            except (OSError, TypeError):
                pass
    return target


if __name__ == "__main__":
    print("frozen:", is_frozen())
    print("resource_dir:", resource_dir())
    print("base_dir    :", base_dir())
