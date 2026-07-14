.PHONY: test scan preflight

test:
	python3 -m unittest discover -s tests -v

scan:
	python3 scripts/secret_scan.py .

preflight:
	./scripts/preflight.sh
