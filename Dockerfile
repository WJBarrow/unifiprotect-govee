FROM python:3.11-slim

WORKDIR /app

# Create log directory
RUN mkdir -p /app/logs

COPY govee_alarm.py .

EXPOSE 8585

# -u: unbuffered stdout/stderr so logs appear immediately
CMD ["python", "-u", "govee_alarm.py"]
