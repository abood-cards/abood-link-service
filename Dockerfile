FROM python:3.12-slim
WORKDIR /app
ENV PORT=8080
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY link_service.py .
EXPOSE 8080
CMD ["python", "link_service.py"]
