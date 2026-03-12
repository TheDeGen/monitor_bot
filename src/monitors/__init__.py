"""Auto-discovery of MonitorPlugin subclasses in this package."""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import List

from src.plugin_base import MonitorPlugin

logger = logging.getLogger(__name__)


def discover_plugins() -> list[MonitorPlugin]:
    """Scan ``src.monitors`` for MonitorPlugin subclasses, instantiate, and return them."""
    plugins: list[MonitorPlugin] = []

    package = importlib.import_module("src.monitors")
    for finder, module_name, is_pkg in pkgutil.iter_modules(package.__path__):
        fqn = f"src.monitors.{module_name}"
        try:
            mod = importlib.import_module(fqn)
        except Exception:
            logger.exception("Failed to import monitor module %s", fqn)
            continue

        for attr_name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, MonitorPlugin)
                and obj is not MonitorPlugin
                and not inspect.isabstract(obj)
            ):
                try:
                    instance = obj()
                    plugins.append(instance)
                    logger.info(
                        "Discovered plugin: %s (interval=%ds)",
                        instance.name,
                        instance.interval,
                    )
                except Exception:
                    logger.exception(
                        "Failed to instantiate plugin %s.%s", fqn, attr_name
                    )

    return plugins
