PYTHON ?= python3
DB_PATH ?= data/grocery_agent.db

.PHONY: db-init db-smoke db-reset slice2-demo baseline-seed baseline-seed-sync suggestion-sync

db-init:
	$(PYTHON) scripts/bootstrap_db.py --db $(DB_PATH)

db-smoke:
	$(PYTHON) scripts/bootstrap_db.py --db $(DB_PATH) --smoke-test

db-reset:
	rm -f $(DB_PATH)
	$(PYTHON) scripts/bootstrap_db.py --db $(DB_PATH) --smoke-test

slice2-demo:
	$(PYTHON) scripts/extract_carrefour_order.py --demo

baseline-seed:
	$(PYTHON) scripts/build_product_baseline.py

baseline-seed-sync:
	$(PYTHON) scripts/build_product_baseline.py --sync-db --db $(DB_PATH)

suggestion-sync:
	$(PYTHON) scripts/build_product_baseline.py --sync-db --db $(DB_PATH)
	sqlite3 $(DB_PATH) "SELECT category_path, brand, preference_label, preference_score FROM brand_preferences ORDER BY category_path, preference_score DESC LIMIT 12;"
