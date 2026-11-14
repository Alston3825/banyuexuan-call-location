FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bycall ./bycall
COPY tools ./tools
ENV BYCALL_DATA=/data
CMD ["python", "-m", "bycall.main"]
