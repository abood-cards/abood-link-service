FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
WORKDIR /app
ENV PORT=8080
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
COPY link_service.py .
EXPOSE 8080
CMD ["python", "link_service.py"]
