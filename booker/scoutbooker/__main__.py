"""`python -m scoutbooker`"""

import logging

from aiohttp import web

from .config import Config
from .server import Booker, make_app

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    cfg = Config.from_env()
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.token:
        logging.warning("BOOKER_TOKEN is not set: the API accepts anyone on the Docker network")
    web.run_app(make_app(Booker(cfg)), host="0.0.0.0", port=cfg.port, access_log=None)


if __name__ == "__main__":
    main()
