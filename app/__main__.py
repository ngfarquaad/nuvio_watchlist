"""Run the Flask development server: python -m app (Docker uses gunicorn instead)."""

import os

from .config import ServerConfig
from .web import create_app

if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=ServerConfig(os.environ).port)
