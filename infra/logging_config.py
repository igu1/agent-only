import logging
import logging.config
import os
from logging.handlers import TimedRotatingFileHandler


class ExactLevelFilter(logging.Filter):
    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == self.level


def configure_logging(*, project_root: str) -> None:
    logs_dir = os.path.join(project_root, 'logs', 'agent')
    os.makedirs(logs_dir, exist_ok=True)

    access_log_path = os.path.join(logs_dir, 'access.log')
    warning_log_path = os.path.join(logs_dir, 'warning.log')
    error_log_path = os.path.join(logs_dir, 'error.log')

    logging_config = {
        'version': 1,
        'disable_existing_loggers': False,
        'filters': {
            'warning_only': {
                '()': 'infra.logging_config.ExactLevelFilter',
                'level': logging.WARNING,
            },
        },
        'formatters': {
            'default': {
                'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
            },
            'access': {
                'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
            },
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'level': 'INFO',
                'formatter': 'default',
            },
            'access_file': {
                'class': 'logging.handlers.TimedRotatingFileHandler',
                'level': 'INFO',
                'formatter': 'access',
                'filename': access_log_path,
                'when': 'midnight',
                'backupCount': 30,
                'encoding': 'utf-8',
                'utc': False,
            },
            'warning_file': {
                'class': 'logging.handlers.TimedRotatingFileHandler',
                'level': 'WARNING',
                'filters': ['warning_only'],
                'formatter': 'default',
                'filename': warning_log_path,
                'when': 'midnight',
                'backupCount': 30,
                'encoding': 'utf-8',
                'utc': False,
            },
            'error_file': {
                'class': 'logging.handlers.TimedRotatingFileHandler',
                'level': 'ERROR',
                'formatter': 'default',
                'filename': error_log_path,
                'when': 'midnight',
                'backupCount': 30,
                'encoding': 'utf-8',
                'utc': False,
            },
        },
        'loggers': {
            'uvicorn.access': {
                'handlers': ['console', 'access_file'],
                'level': 'INFO',
                'propagate': False,
            },
            'uvicorn.error': {
                'handlers': ['console', 'warning_file', 'error_file'],
                'level': 'INFO',
                'propagate': False,
            },
            'agent': {
                'handlers': ['console', 'warning_file', 'error_file'],
                'level': 'INFO',
                'propagate': False,
            },
        },
        'root': {
            'handlers': ['console', 'warning_file', 'error_file'],
            'level': 'INFO',
        },
    }

    for handler_name in ('access_file', 'warning_file', 'error_file'):
        handler_cfg = logging_config['handlers'][handler_name]
        handler = TimedRotatingFileHandler(
            filename=handler_cfg['filename'],
            when=handler_cfg['when'],
            backupCount=handler_cfg['backupCount'],
            encoding=handler_cfg['encoding'],
            utc=handler_cfg.get('utc', False),
        )
        handler.suffix = '%Y-%m-%d'
        handler.close()

    logging.config.dictConfig(logging_config)
