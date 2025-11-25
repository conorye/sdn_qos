'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 15:39:46
LastEditTime: 2025-11-25 15:39:53
FilePath: /sdn_qos/controller/logger.py
Description: 

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# logger.py
import logging
import logging.handlers
import json
import time
from typing import Optional, Dict

class JSONFormatter(logging.Formatter):
    def format(self, record):
        base = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # allow extra fields passed via logger.bind(...)
        if hasattr(record, "extra") and isinstance(record.extra, dict):
            base.update(record.extra)
        # include exception info if exists
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)

class SimpleLogger:
    """
    SimpleLogger: configured once, then imported/used across modules.
    Usage:
      logger = SimpleLogger.get_logger("scheduler")
      logger.info("flow_allowed", extra={"flow_id": 1001, "status":"allowed"})
    """

    _initialized = False
    _loggers: Dict[str, logging.Logger] = {}

    @classmethod
    def init(cls,
             level=logging.INFO,
             log_file: Optional[str] = "/var/log/global_scheduler.log",
             max_bytes: int = 10 * 1024 * 1024,
             backup_count: int = 5,
             json_format: bool = True):
        if cls._initialized:
            return
        fmt = JSONFormatter() if json_format else logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s")
        root = logging.getLogger()
        root.setLevel(level)
        # console handler
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)
        # rotating file handler (best-effort - fallback to local file)
        try:
            fh = logging.handlers.RotatingFileHandler(
                filename=log_file, maxBytes=max_bytes, backupCount=backup_count)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception:
            # fallback: ignore file handler errors but keep console output
            root.warning("SimpleLogger: cannot create file handler %s", log_file)
        cls._initialized = True

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        if not cls._initialized:
            # default init to console only
            cls.init()
        if name in cls._loggers:
            return cls._loggers[name]
        logger = logging.getLogger(name)
        cls._loggers[name] = logger
        return logger

# convenience function to add structured extra
def log_with_extra(logger, level, msg, **extra):
    if extra:
        logger.log(level, msg, extra={"extra": extra})
    else:
        logger.log(level, msg)
