.PHONY: test estimate scan preflight

test:
	python3 -m unittest discover -s tests -v
	node --test tests/*.test.mjs

estimate:
	python3 cost_estimator.py

scan:
	python3 scripts/secret_scan.py .

preflight:
	./scripts/preflight.sh
