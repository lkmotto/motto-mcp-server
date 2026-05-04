FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_server ./mcp_server
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

ENV PORT=8080
EXPOSE 8080

CMD ["motto-mcp-server"]
