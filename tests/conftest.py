import os

import pytest

from sitewatch import config


@pytest.fixture(autouse=True)
def isolate_environment():
    """load_dotenv пишет прямо в os.environ — после каждого теста возвращаем окружение как было."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    config._loaded_from_file.clear()
