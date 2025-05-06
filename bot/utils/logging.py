import logging
import os

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL, logging.INFO)

# Use consistent logger name across all modules
logger = logging.getLogger("RoboDoze")
logger.setLevel(LOG_LEVEL)

formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")

# File handler
file_handler = logging.FileHandler("RoboDoze.log", mode="a")
file_handler.setFormatter(formatter)
file_handler.setLevel(LOG_LEVEL)

# Stream handler (prints to console)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
stream_handler.setLevel(LOG_LEVEL)

# Add handlers once
if not logger.hasHandlers():
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)