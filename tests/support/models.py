"""Every table model in the codebase, imported so their metadata is complete."""

import importlib
import importlib.util
import pkgutil

from sqlalchemy import MetaData

import corridor
from corridor.platform.db import Base


def all_metadata() -> MetaData:
    for module in pkgutil.iter_modules(corridor.__path__):
        name = f"corridor.{module.name}.models"
        if module.ispkg and importlib.util.find_spec(name) is not None:
            importlib.import_module(name)
    return Base.metadata
