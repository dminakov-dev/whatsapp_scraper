"""Run the mock bridge: python -m bridge_mock"""
import uvicorn

from collector.log import setup_logging

from .app import create_app
from .settings import load_settings


def main() -> None:
    settings = load_settings()
    setup_logging("bridge_mock", settings.log_dir, settings.log_level)
    # log_config=None: uvicorn logs go through our root handlers (file + stdout).
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
