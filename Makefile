.PHONY: monthly-shadow-monitor monthly-shadow-check

monthly-shadow-monitor:
	python3 run_monthly_shadow_monitor.py

monthly-shadow-check:
	python3 -m unittest discover -s tests
