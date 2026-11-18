FROM python:3.12-slim

# TZ: o "hoje" do container é o do negócio (o app também usa APP_TIMEZONE, sem depender do sistema)
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=America/Sao_Paulo APP_TIMEZONE=America/Sao_Paulo
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# usuário sem privilégios; /dados guarda o estado do agente (volume do bot)
RUN useradd --create-home --uid 1000 app && mkdir -p /dados && chown app:app /dados

COPY app ./app
COPY scripts ./scripts

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
