"""``python -m corridor.worker`` runs a worker."""

from corridor.platform.config import load_settings
from corridor.worker.main import run

if __name__ == "__main__":
    run(load_settings())
