FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

ENV PORT=8080
EXPOSE 8080

# Single container: FastAPI shell serves /agui (SSE) + the Dash app at /.
CMD ["sh", "-c", "uvicorn app.server:app --host 0.0.0.0 --port ${PORT}"]
