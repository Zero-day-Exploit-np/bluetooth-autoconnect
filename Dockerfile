FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY README.md .
COPY pyproject.toml .

RUN python -m pip install --no-cache-dir .

CMD ["bluetooth-autoconnect", "--daemon", "--verbose"]
