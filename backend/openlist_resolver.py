#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STRM 库相对路径 + 媒体文件名 → OpenList HTTP URL。"""
from urllib.parse import quote


def resolve(media_url_root: str, strm_lib_relative: str, media_filename: str) -> str:
    if not media_url_root or not strm_lib_relative or not media_filename:
        return ""
    # 去 .strm 后缀，取目录部分（媒体文件与 STRM 同目录）
    rel = strm_lib_relative[:-5] if strm_lib_relative.endswith(".strm") else strm_lib_relative
    dir_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    base = media_url_root.rstrip("/")
    path = f"{base}/{dir_rel}/{media_filename}" if dir_rel else f"{base}/{media_filename}"
    # 对路径部分做百分号编码，保留 scheme 分隔与查询字符
    head, _, tail = path.partition("://")
    return f"{head}://{quote(tail, safe='/:?=&%')}"
