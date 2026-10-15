FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
# La réplica del portal vive en la misma app; el conector la consulta en el mismo puerto.
CMD ["sh", "-c", "SIIAA_BASE_URL=${SIIAA_BASE_URL:-http://127.0.0.1:${PORT}/siiaa} uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
