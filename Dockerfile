FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/scw_registry_cleaner scw_registry_cleaner
COPY entrypoint.sh .

ENTRYPOINT [ "/app/entrypoint.sh" ]

CMD [ "--help" ]
