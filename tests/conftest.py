from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def sample():
    def load(name: str) -> str:
        return next(SAMPLES.glob(f"{name}*.eml")).read_text(encoding="utf-8")
    return load
