import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

@pytest.fixture
def client():
    from app import app
    app.testing = True
    with app.test_client() as client:
        yield client
