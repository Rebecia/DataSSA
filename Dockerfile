
FROM python:3.11-slim

WORKDIR /app

# Install deps first for better cache usage
COPY vendor/nanobot-main /app/vendor/nanobot-main
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the rest of the project
COPY . /app

ENV PATH="/app/bin:${PATH}"
ENV DB_PATH="./data/business.db"
ENV DB_READONLY="true"

CMD ["database-agent", "gateway"]
