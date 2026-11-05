"""``python -m ttsgrab`` / ``ttsgrab`` entry point."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="ttsgrab", description="Unified TTS gateway")
    parser.add_argument("--host", default=None, help="Bind address (default: settings.host)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev)")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    if args.config:
        os.environ["TTSGRAB_CONFIG"] = args.config
    if args.log_level:
        os.environ["TTSGRAB_LOG_LEVEL"] = args.log_level

    import uvicorn

    from ttsgrab.settings import load_settings

    settings = load_settings(args.config)
    host = args.host or settings.host
    port = args.port or settings.port

    if args.reload:
        uvicorn.run(
            "ttsgrab.main:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=["src"],
            log_level=settings.log_level.lower(),
        )
    else:
        uvicorn.run(
            "ttsgrab.main:app",
            host=host,
            port=port,
            log_level=settings.log_level.lower(),
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )


if __name__ == "__main__":
    main()
