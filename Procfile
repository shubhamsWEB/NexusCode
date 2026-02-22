web: uvicorn src.api.app:app --host 0.0.0.0 --port $PORT
worker: rq worker indexing --url $REDIS_URL
dashboard: streamlit run src/ui/dashboard.py --server.port $DASHBOARD_PORT --server.address 0.0.0.0
