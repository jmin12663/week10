FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install flask --trusted-host pypi.org --trusted-host files.pythonhosted.org
COPY app.py .
EXPOSE 5000
CMD ["python", "app.py"]
