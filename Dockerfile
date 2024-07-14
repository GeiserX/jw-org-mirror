FROM python:3.11-bookworm 
# 3.12 Not working, lxml not compiling

USER root

RUN apt-get update && \
    apt-get install -y libxml2-dev libxslt-dev build-essential libssl-dev libffi-dev && \
    apt-get clean

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
RUN playwright install 

COPY . .
EXPOSE 80
CMD ["python3", "-u", "src/main.py"]