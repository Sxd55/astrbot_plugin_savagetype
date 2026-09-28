"""AstrBot 插件标准发布打包脚本 (savagetype)。"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

MANDATORY_ENTRIES = (
    "astrbot_plugin_savagetype/logo.png",
    "astrbot_plugin_savagetype/metadata.yaml",
    "astrbot_plugin_savagetype/main.py",
    "astrbot_plugin_savagetype/requirements.txt",
    "astrbot_plugin_savagetype/_conf_schema.json",
    "astrbot_plugin_savagetype/savagetype/__init__.py",
    "astrbot_plugin_savagetype/savagetype/service.py",
    "astrbot_plugin_savagetype/savagetype/store.py",
    "astrbot_plugin_savagetype/savagetype/contexthistory.py",
    "astrbot_plugin_savagetype/savagetype/groupidentity.py",
    "astrbot_plugin_savagetype/savagetype/autoclean.py",
    "astrbot_plugin_savagetype/savagetype/builtinallow.py",
)

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "data",
    "node_modules",
}

EXCLUDE_EXTS = {
    ".pyc",
    ".pyo",
    ".pyd",
}


def build_plugin_zip() -> Path:
    plugin_dir = Path(__file__).resolve().parent
    parent_dir = plugin_dir.parent
    output_zip = parent_dir / "astrbot_plugin_savagetype.zip"

    logo_file = plugin_dir / "logo.png"
    if not logo_file.is_file():
        raise FileNotFoundError(f"插件核心图标缺失: {logo_file}")

    if output_zip.exists():
        output_zip.unlink()

    print(f"正在打包插件: {plugin_dir.name} -> {output_zip.name} ...")

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(plugin_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

            for f in files:
                if any(f.endswith(ext) for ext in EXCLUDE_EXTS):
                    continue

                file_path = Path(root) / f
                arcname = file_path.relative_to(parent_dir).as_posix()
                zf.write(file_path, arcname=arcname)

    with zipfile.ZipFile(output_zip, "r") as zf:
        names = set(zf.namelist())
        for required in MANDATORY_ENTRIES:
            if required not in names:
                raise AssertionError(f"打包完整性校验失败，缺少关键文件: {required}")

    size_kb = output_zip.stat().st_size / 1024
    print(f"打包成功！文件数: {len(names)}, 大小: {size_kb:.2f} KB")

    backup_zip = parent_dir.parent / "savage - 副本" / "astrbot_plugin_savagetype.zip"
    if backup_zip.parent.exists():
        shutil.copy2(output_zip, backup_zip)
        print(f"已同步更新备份包: {backup_zip}")

    return output_zip


if __name__ == "__main__":
    build_plugin_zip()
