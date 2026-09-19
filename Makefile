.PHONY: data ar ap score run test test-api docker clean

data:
	python data/generate_data.py

ar:
	python reconcile_ar.py

ap:
	python reconcile.py

score:
	python score.py

run:
	uvicorn app:app --reload --port 8000

# Full offline check: generate -> reconcile AR -> reconcile AP -> score -> validate.
# Fails on any misclassification.
test:
	python run_all.py

# HTTP check against a server already listening on :8000
test-api:
	python test_api.py

docker:
	docker build -t real-estate-recon-agent .
	docker run --rm -p 8000:8000 real-estate-recon-agent

clean:
	rm -rf __pycache__ data/__pycache__ ar_results.csv ap_results.csv
