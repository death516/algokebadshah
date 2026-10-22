.PHONY: test smoke run install lint clean

install:
	pip install -r requirements.txt

test:
	pytest tests/ -v

smoke:
	python scripts/smoke_test.py

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

lint:
	python -m compileall app backtest tests scripts

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
