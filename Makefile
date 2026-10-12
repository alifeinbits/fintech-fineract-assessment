.PHONY: test-python simulate seed

test-python:
	cd mock-erp && python -m pytest
	cd integration-service && python -m pytest

simulate:
	cd integration-service && python ../scripts/simulate.py | tee ../evidence/simulations.txt

seed:
	python scripts/seed_fineract.py
