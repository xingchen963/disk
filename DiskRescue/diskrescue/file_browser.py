"""Read-only directory browsing helpers for mounted Windows volumes and HFS+ catalogs."""

from __future__ import annotations

import os
import posixpath
import stat
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_MAX_ENTRIES = 5000
DEFAULT_PREVIEW_BYTES = 128 * 1024
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_path_within_root(path: str, root_path: str) -> bool:
    """Return whether path resolves lexically inside the selected volume root."""
    try:
        root = os.path.normcase(os.path.abspath(os.path.normpath(root_path)))
        candidate = os.path.normcase(os.path.abspath(os.path.normpath(path)))
        return os.path.commonpath([root, candidate]) == root
    except (OSError, ValueError):
        return False


def _is_reparse_or_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        info = entry.stat(follow_symlinks=False)
        attributes = getattr(info, "st_file_attributes", 0)
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True


def _path_has_reparse_component(path: str, root_path: str) -> bool:
    """Reject a path whose components below the selected root contain a link/junction."""
    if not is_path_within_root(path, root_path):
        return True
    root = os.path.abspath(os.path.normpath(root_path))
    target = os.path.abspath(os.path.normpath(path))
    try:
        relative = os.path.relpath(target, root)
    except ValueError:
        return True
    if relative in ("", "."):
        return False

    current = root
    for component in relative.split(os.sep):
        if component in ("", "."):
            continue
        current = os.path.join(current, component)
        try:
            info = os.lstat(current)
        except OSError:
            # Missing or inaccessible components are not followed. The actual
            # directory/file operation will report the concrete OS error.
            return False
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    return False


def list_windows_directory(
    path: str,
    root_path: str,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict[str, Any]:
    """List one directory without following links or leaving the selected volume."""
    if not is_path_within_root(path, root_path):
        return {"path": path, "entries": [], "error": "访问路径超出当前分区范围，已阻止。"}
    if _path_has_reparse_component(path, root_path):
        return {"path": path, "entries": [], "error": "路径包含符号链接或 Windows 重解析点，已阻止跟随。"}
    if max_entries <= 0:
        return {"path": path, "entries": [], "error": "目录读取上限必须大于零。"}

    target = os.path.abspath(os.path.normpath(path))
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    truncated = False

    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                if len(entries) >= max_entries:
                    truncated = True
                    break

                try:
                    info = entry.stat(follow_symlinks=False)
                    attributes = getattr(info, "st_file_attributes", 0)
                    is_link = _is_reparse_or_link(entry)
                    is_dir = False if is_link else entry.is_dir(follow_symlinks=False)
                    is_file = False if is_link else entry.is_file(follow_symlinks=False)
                    if is_link:
                        kind = "链接 / 重解析点（不跟随）"
                    elif is_dir:
                        kind = "文件夹"
                    elif is_file:
                        kind = "文件"
                    else:
                        kind = "其他"

                    flags = []
                    if entry.name.startswith(".") or attributes & _FILE_ATTRIBUTE_HIDDEN:
                        flags.append("隐藏")
                    if attributes & _FILE_ATTRIBUTE_SYSTEM:
                        flags.append("系统")
                    if not (info.st_mode & stat.S_IWUSR):
                        flags.append("只读")
                    if is_link:
                        flags.append("不跟随链接")

                    modified = ""
                    try:
                        modified = datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                    except (OverflowError, OSError, ValueError):
                        modified = "未知"

                    entries.append({
                        "name": entry.name,
                        "kind": kind,
                        "path": entry.path,
                        "size": int(info.st_size) if is_file else None,
                        "modified": modified,
                        "attributes": "；".join(flags) or "正常",
                        "is_dir": bool(is_dir),
                        "is_file": bool(is_file),
                        "is_link": bool(is_link),
                    })
                except OSError as exc:
                    warnings.append(f"{entry.name}：{exc}")

    except OSError as exc:
        return {
            "path": target,
            "entries": [],
            "warnings": warnings,
            "truncated": False,
            "error": f"无法读取目录：{exc}",
        }

    entries.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
    return {
        "path": target,
        "entries": entries,
        "warnings": warnings,
        "truncated": truncated,
        "error": "",
    }


def catalog_directory_entries(
    catalog: dict[str, Any] | None,
    current_path: str = "/",
) -> list[dict[str, Any]]:
    """Return immediate children from the flat HFS+ / HFSX Catalog B-tree listing."""
    catalog = catalog or {}
    normalized_current = posixpath.normpath("/" + str(current_path or "/").lstrip("/"))
    if normalized_current == ".":
        normalized_current = "/"

    result = []
    for entry in catalog.get("entries", []) or []:
        raw_path = str(entry.get("path") or "")
        if not raw_path.startswith("/"):
            continue
        normalized = posixpath.normpath(raw_path)
        if normalized == normalized_current or normalized == "/":
            continue
        parent = posixpath.dirname(normalized) or "/"
        if parent != normalized_current:
            continue

        kind = str(entry.get("kind") or "")
        is_dir = kind == "目录"
        result.append({
            "name": str(entry.get("name") or posixpath.basename(normalized)),
            "kind": "文件夹" if is_dir else "文件",
            "path": normalized,
            "size": entry.get("size") if not is_dir else None,
            "modified": "文件系统未提供",
            "attributes": "HFS+ 目录元数据",
            "id": entry.get("id"),
            "child_count": entry.get("child_count"),
            "is_dir": is_dir,
            "is_file": not is_dir,
            "is_link": False,
        })
    result.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
    return result


def read_text_preview(
    path: str,
    root_path: str,
    max_bytes: int = DEFAULT_PREVIEW_BYTES,
) -> dict[str, Any]:
    """Read a strictly bounded preview of a mounted-volume file; never write to disk."""
    if not is_path_within_root(path, root_path):
        return {"error": "访问路径超出当前分区范围，已阻止。", "text": ""}
    if _path_has_reparse_component(path, root_path):
        return {"error": "路径包含符号链接或 Windows 重解析点，已阻止跟随。", "text": ""}

    if max_bytes <= 0:
        return {"error": "预览长度必须大于零。", "text": ""}

    target = os.path.abspath(os.path.normpath(path))
    try:
        info = os.lstat(target)
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            return {"error": "为避免跟随链接或重解析点，不能预览该项目。", "text": ""}
        if not stat.S_ISREG(info.st_mode):
            return {"error": "所选项目不是普通文件。", "text": ""}

        with open(target, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        return {"error": f"无法读取文件预览：{exc}", "text": ""}

    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    if b"\x00" in data:
        hex_text = " ".join(f"{byte:02X}" for byte in data[:512])
        return {
            "error": "检测到二进制内容，仅显示前 512 字节十六进制；没有加载完整文件。",
            "text": hex_text,
            "binary": True,
            "truncated": truncated or len(data) > 512,
        }

    decoded = None
    encoding_used = ""
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            decoded = data.decode(encoding)
            encoding_used = encoding
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        decoded = data.decode("utf-8", errors="replace")
        encoding_used = "UTF-8（替换无效字符）"

    note = f"编码：{encoding_used}；预览上限：{max_bytes:,} 字节"
    if truncated:
        note += "；文件较大，内容已截断"
    return {
        "error": "",
        "text": decoded,
        "binary": False,
        "encoding": encoding_used,
        "truncated": truncated,
        "note": note,
    }


def windows_root_for_partition(partition: dict[str, Any]) -> str:
    """Find a mounted root for a Windows partition; prefer a drive-letter path."""
    drive = str(partition.get("DriveLetter") or "").strip().rstrip(":")
    if drive:
        return f"{drive}:\\"

    access_paths = partition.get("AccessPaths") or []
    if isinstance(access_paths, str):
        access_paths = [access_paths]
    for candidate in access_paths:
        text = str(candidate or "").strip()
        if len(text) >= 3 and text[1:3] == ":\\":
            return text
        if text.startswith("\\\\?\\Volume{") and text.endswith("\\"):
            return text
    return ""
