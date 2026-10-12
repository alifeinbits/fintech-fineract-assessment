.PHONY: test-python test-mypy simulate seed

test-python:
	cd mock-erp && python -m pytest
	cd integration-service && python -m pytest
	$(MAKE) test-mypy

test-mypy:
	cd mock-erp && python -m mypy app
	cd integration-service && python -m mypy app

simulate:
	cd integration-service && python ../scripts/simulate.py | tee ../evidence/simulations.txt

seed:
	python scripts/seed_fineract.py
