"""Logging utilities for signing components."""

import logging


class InstanceLevelAdapter(logging.LoggerAdapter):
    """Checks instance_level before logging and passes instance_name to formatter."""

    def log(self, level, msg, *args, **kwargs):
        if level >= self.extra.get("instance_level", logging.INFO):
            super().log(level, msg, *args, **kwargs)
