"""``python -m corridor_sim`` serves the simulators, the same way on every platform."""

import uvicorn

from corridor_sim.app import create_app
from corridor_sim.settings import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, server_header=False)


if __name__ == "__main__":
    main()
