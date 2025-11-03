# app.py
from flask import Flask, request, jsonify
import requests
import os
import io
import json
import time
import threading
import logging
import uuid
import re
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# ====================================
# CONFIGURAÇÕES E VARIÁVEIS
# ====================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
NEW_NUMBER = os.environ.get("NEW_NUMBER")
FORWARD_NUMBER = os.environ.get("FORWARD_NUMBER")
CONTACTS_URL = os.environ.get("CONTACTS_URL")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")  # usado para enviar lembrete automático

LOG_FILE = "app.log"
ALLOWED_MEDIA_TYPES = ["image", "document", "audio"]
IGNORED_TYPES = ["status", "sticker", "reaction", "location", "unknown", "video"]

# REMINDER CONFIG
# horas antes do fim das 24h em que o lembrete é enviado (ex.: 1 = envia quando restar 1 hora)
REMINDER_HOURS_BEFORE = int(os.environ.get("REMINDER_HOURS_BEFORE", "1"))
# para quem enviar o lembrete (padrão usa FORWARD_NUMBER)
REMINDER_TO = os.environ.get("REMINDER_TO", FORWARD_NUMBER)

# intervalo de checagem do worker em segundos
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))  # 5 minutos por padrão

# ====================================
# LOGGING
# ====================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)

def log(level, message, data=None):
    try:
        s = json.dumps(data, ensure_ascii=False) if data is not None else ""
    except Exception:
        s = str(data)
    getattr(logging, level)(f"{message} | data={s}")

# ====================================
# CONTATOS (simples carregamento de CSV/texto)
# ====================================
def normalize_number(raw):
    raw = re.sub(r"\D", "", raw or "")
    if not raw:
        return None
    if raw.startswith("55") and len(raw) > 13:
        raw = raw[-13:]
    if not raw.startswith("55"):
        raw = "55" + raw[-11:]
    return raw

def load_contacts_from_drive():
    contacts = {}
    if not CONTACTS_URL:
        log("warning", "CONTACTS_URL ausente")
        return contacts

    try:
        log("info", "Baixando contatos do Google Drive", {"url": CONTACTS_URL})
        resp = requests.get(CONTACTS_URL, timeout=30)
        resp.raise_for_status()
        text = resp.content.decode("utf-8", errors="ignore")

        raw_lines = text.splitlines()
        total_lines = len(raw_lines)
        for line in raw_lines:
            # extrai números e um nome aproximado
            phones = re.findall(r"\+?\d[\d\s\-\(\)]{6,}\d", line)
            if not phones:
                continue
            # pega sequência de palavras como nome (heurística simples)
            possible_names = re.findall(r"[A-Za-zÀ-ÿ0-9\-\.\&\s]{1,60}", line)
            name = None
            # achar primeira parte que não seja apenas número
            for p in possible_names:
                if not re.search(r"\d", p.strip()):
                    name = p.strip()
                    break
            if not name:
                name = "Desconhecido"
            for phone in phones:
                n = normalize_number(phone)
                if n:
                    contacts[n] = name

        log("info", "Contatos carregados e normalizados", {
            "total_linhas": total_lines,
            "total_contatos": len(contacts)
        })
    except Exception as e:
        log("error", "Falha ao processar contatos", {"error": str(e)})

    return contacts

CONTACTS = load_contacts_from_drive()

# ====================================
# FUNÇÕES AUXILIARES (envio)
# ====================================
def send_message(phone_number_id, to, message):
    """Envia mensagem via Graph API. Retorna resp ou None."""
    if not ACCESS_TOKEN:
        log("error", "ACCESS_TOKEN ausente ao tentar enviar mensagem")
        return None
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to}
    payload.update(message)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        try:
            status = resp.status_code
            text = resp.text
        except Exception:
            status = None
            text = None
        log("info", "Send message result", {"to": to, "status": status, "response": text})
        return resp
    except Exception as e:
        log("error", "Erro ao enviar", {"error": str(e)})
        return None

def format_phone(num):
    digits = "".join(ch for ch in (num or "") if ch.isdigit())
    if len(digits) < 10:
        return digits
    return f"{digits[:2]} {digits[2:4]} {digits[4:]}"

# ====================================
# CONTROLE DE ATIVIDADE / SESSÃO META (24H)
# ====================================
# Última atividade observada (UTC)
LAST_ACTIVITY = datetime.utcnow()
# Timestamp do último lembrete enviado (para evitar spam)
LAST_REMINDER_SENT = None
# flag para permitir lógica simplificada de match (opcional)
USE_LAST8_MATCH = os.environ.get("USE_LAST8_MATCH", "true").lower() in ("1", "true", "yes")

def update_activity():
    global LAST_ACTIVITY, LAST_REMINDER_SENT
    LAST_ACTIVITY = datetime.utcnow()
    # reinicia a janela de lembrete (permitir novo lembrete no futuro)
    LAST_REMINDER_SENT = None

def check_meta_session():
    """Thread que checa inatividade e envia lembrete se necessário."""
    global LAST_ACTIVITY, LAST_REMINDER_SENT
    while True:
        try:
            now = datetime.utcnow()
            delta = now - LAST_ACTIVITY
            seconds_since = delta.total_seconds()
            # quando restar menos que REMINDER_HOURS_BEFORE horas para completar 24h:
            threshold_seconds = (24 - REMINDER_HOURS_BEFORE) * 3600
            if seconds_since >= threshold_seconds:
                # só envia se ainda não enviou desde a última atividade
                if not LAST_REMINDER_SENT:
                    if not PHONE_NUMBER_ID:
                        log("warning", "Sessão próxima do limite, mas PHONE_NUMBER_ID não definido. Apenas logando o evento.",
                            {"last_activity": LAST_ACTIVITY.isoformat(), "hours_since": seconds_since / 3600.0})
                        LAST_REMINDER_SENT = now.isoformat()
                    else:
                        # envia lembrete para REMINDER_TO (remova + se houver)
                        to = REMINDER_TO.replace("+", "")
                        msg = {"text": {"body": "⚠️ Atenção — a janela de 24h da API está próxima de expirar. Envie qualquer mensagem para renovar a sessão."}}
                        resp = send_message(PHONE_NUMBER_ID, to, msg)
                        if resp is None:
                            log("error", "Falha ao enviar lembrete de sessão", {"to": to})
                        else:
                            log("warning", "Lembrete de sessão enviado", {"to": to, "status_code": getattr(resp, "status_code", None)})
                        LAST_REMINDER_SENT = now.isoformat()
            # dorme um intervalo curto e re-verifica
        except Exception as e:
            log("error", "check_meta_session exception", {"error": str(e)})
        time.sleep(CHECK_INTERVAL_SECONDS)

# inicia thread de monitoramento
threading.Thread(target=check_meta_session, daemon=True).start()

# ====================================
# WEBHOOK
# ====================================
@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        log("info", "Webhook verificado")
        return challenge
    log("warning", "Falha na verificação do webhook", {"received_token": token})
    return "Erro de verificação", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    # atualiza atividade apenas se o remetente for o seu número
try:
    payload = request.get_json()
    sender_id = payload.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [{}])[0].get("from")
    if sender_id and sender_id.endswith("97216766"):  # últimos dígitos do seu número
        update_activity()
except Exception:
    pass

    event_id = str(uuid.uuid4())[:8]
    raw = request.get_data(as_text=True)
    log("info", "Webhook recebido (raw payload)", {"payload": raw[:200] + ("...(truncated)" if len(raw) > 200 else "")})

    try:
        data = request.get_json()
    except Exception as e:
        log("error", "Falha ao parsear JSON do webhook", {"error": str(e)})
        return "ok", 200

    if not data:
        return "ok", 200

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue

            msg = messages[0]
            sender = msg.get("from")
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            msg_type = msg.get("type", "text")

            if msg_type in IGNORED_TYPES or not sender or not phone_number_id:
                continue

            # normaliza sender para buscar no dicionário
            norm_sender = re.sub(r"\D", "", sender or "")

            # 1) busca exata com DDI+DDD...
            name = CONTACTS.get(norm_sender, None)

            # 2) (opcional) busca sem o '55' e sem o DDD — match por últimos 8 dígitos
            if not name and USE_LAST8_MATCH:
                last8 = norm_sender[-8:]
                # procura uma chave em CONTACTS que termine com esses 8 dígitos
                for k, v in CONTACTS.items():
                    if k.endswith(last8):
                        name = v
                        break

            if not name:
                name = "Desconhecido"
                # log amostra de chaves para debug
                sample_keys = list(CONTACTS.keys())[:5]
                log("info", "Contato não identificado", {"sender": sender, "amostra": sample_keys})

            # extrai texto
            text = ""
            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
            elif msg_type == "contacts":
                cts = msg.get("contacts", [])
                text = " | ".join(f"{c.get('name', {}).get('formatted_name', '')} {c.get('phones', [{}])[0].get('phone', '')}" for c in cts)

            # responder ao remetente com mensagem padrão
            reply = (
                f"Olá! Este número não está mais ativo.\n"
                f"Por favor, salve meu novo contato e me chame lá:\n"
                f"👉 https://wa.me/{NEW_NUMBER.replace('+', '') if NEW_NUMBER else ''}"
            )
            send_message(phone_number_id, sender, {"text": {"body": reply}})

            # encaminhar resumo para FORWARD_NUMBER
            hora_local = datetime.now(timezone(timedelta(hours=-3))).strftime("%H:%M:%S")
            formatted_phone = format_phone(sender)
            forward_text = f"👤 {name}\n📱 {formatted_phone}\n🕓 {hora_local}\n💬 {text or '(mensagem de mídia)'}"
            send_message(phone_number_id, FORWARD_NUMBER.replace("+", ""), {"text": {"body": forward_text}})

    return "ok", 200

# ====================================
# HEALTH CHECK
# ====================================
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "ok",
        "total_contacts": len(CONTACTS),
        "last_activity_utc": LAST_ACTIVITY.isoformat()
    })

# ====================================
# STARTUP
# ====================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("info", "➡️ Aplicação iniciando", {"port": port})
    app.run(host="0.0.0.0", port=port)

