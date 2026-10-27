# -*- coding: utf-8 -*-
"""B 站录播姬后端：录制直播流 → 本地分段 → 生成封面/弹幕 → 投稿队列。

只用标准库（本机命令行走不了 HTTPS 代理链，但 Python 自带 ssl 正常）：
  - 抓流用 urllib（不依赖 requests）
  - 封装备份用 ffmpeg（只处理本地文件）
"""
__version__ = '1.0.0'
