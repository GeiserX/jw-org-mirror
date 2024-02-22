FROM python:3.11-slim 
# lxml not working in 3.12 gcc build-essential pkg-config python3-dev 
WORKDIR /app
RUN apt-get update && \
    apt-get install -y libxml2-dev libxslt-dev chromium && \
    apt-get clean
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 80
CMD ["python", "-u", "src/main.py"]