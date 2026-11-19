#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Local development launcher. WSGI deployments import web.create_app directly."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web import create_app

def main(argv=None):
    parser = argparse.ArgumentParser(description="新闻机器人 Web 管理面板（本地开发入口）")
    parser.add_argument("--port", type=int, default=5000, help="监听端口（默认 5000）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--config", default=None, help="所选配置文件路径")
    parser.add_argument("--debug", action="store_true", help="仅允许在回环地址启用调试模式")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间。")
    if args.debug and args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("调试模式只能监听回环地址。远程部署请使用 WSGI、HTTPS 和访问控制。")
    app = create_app(args.config)
    print(f"本地开发面板：http://{args.host}:{args.port}")
    print("管理口令来自 WEB_PASSWORD 或所选配置中的 web_password 哈希；未配置时拒绝登录。")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
