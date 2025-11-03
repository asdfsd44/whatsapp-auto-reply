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
import csv
import re
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# ====================================
# CONFIGURAÇÕES E VARIÁVEIS
# ====================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
NEW_NUMBER = os.environ.get("NEW_NUMBER")
NEW_NAME = os.environ.get("NEW_NAME", "Novo Contato")
FORWARD_NUMBER = os.environ.get("FORWARD_NUMBER", "+5534997216766")
CONTACTS_URL = os.environ.get("CONTACTS_URL")

REMETENTES_FILE = "remetentes.txt"
RETRY_FILE = "retries.json"
LOG_FILE = "app.log"

ALLOWED_MEDIA_TYPES = ["image", "document", "audio"]
IGNORED_TYPES = ["status", "sticker", "reaction", "location", "unknown", "video"]

MAX_RETRIES = 5
RETRY_INTERVAL_SECONDS = 60
MAX_LOG_FIELD = 2000

# ====================================
# LOGGING
# ====================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)

def log(level, message, data=None, event_id=None):
    prefix = f"[{event_id}] " if event_id else ""
    details = ""
    if data:
        try:
            s = json.dumps(data, ensure_ascii=False)
        except Exception:
            s = str(data)
        if len(s) > MAX_LOG_FIELD:
            s = s[:MAX_LOG_FIELD] + "...(truncated)"
        details = f" | data={s}"
    getattr(logging, level)(prefix + message + details)

# ====================================
# UTILITÁRIOS
# ====================================
def now_iso():
    return datetime.utcnow().isoformat()

def short_json(obj, max_len=MAX_LOG_FIELD):
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return s if len(s) <= max_len else s[:max_len] + "...(truncated)"

def format_phone(num):
    digits = "".join(ch for ch in (num or "") if ch.isdigit())
    if len(digits) < 10:
        return digits
    ddi = digits[:2]
    ddd = digits[2:4]
    base = digits[4:]
    return f"{ddi} {ddd} {base}"

# ====================================
# CONTATOS VIA GOOGLE DRIVE
# ====================================
def normalize_number(raw):
    if not raw:
        return None
    raw = re.sub(r'\D', '', raw)
    if len(raw) < 10:
        return None
    if raw.startswith('55') and len(raw) > 13:
        raw = raw[-13:]
    if not raw.startswith('55'):
        raw = '55' + raw[-11:]
    return raw

def load_contacts_from_drive(url_or_path=None):
    contacts = {}
    url_or_path = url_or_path or CONTACTS_URL
    if not url_or_path:
        log("warning", "Nenhuma fonte de contatos definida (CONTACTS_URL ausente)")
        return contacts
    try:
        if url_or_path.startswith("http"):
            log("info", "Baixando contatos do Google Drive", {"url": url_or_path})
            resp = requests.get(url_or_path, timeout=20)
            resp.raise_for_status()
            content = io.StringIO(resp.content.decode("utf-8", errors="ignore"))
            reader = csv.reader(content)
        else:
            reader = csv.reader(open(url_or_path, encoding="utf-8", errors="ignore"))
        for row in reader:
            if not row:
                continue
            name = next((c.strip() for c in row if c and not re.search(r'\d', c)), None)
            phones = re.findall(r'\+?\d[\d\s\-()]*\d', ' '.join(row))
            for p in phones:
                n = normalize_number(p)
                if n:
                    contacts[n] = name or "Desconhecido"
        log("info", "Contatos carregados com sucesso", {"total": len(contacts)})
    except Exception as e:
        log("error", "Falha ao carregar contatos", {"error": str(e)})
    return contacts

CONTACTS = load_contacts_from_drive()

# ====================================
# ENVIO DE MENSAGENS
# ====================================
def send_message(phone_number_id, to, message):
    event_id = str(uuid.uuid4())[:8]
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to}
    payload.update(message)
    log("info", "Tentando enviar mensagem", {"url": url, "to": to, "payload": payload}, event_id)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        log("info", "Resposta do Graph API", {"status_code": resp.status_code, "response_text": resp.text}, event_id)
        return resp
    except Exception as e:
        log("error", "Erro ao enviar mensagem", {"error": str(e)}, event_id)
        return None

def forward_text(phone_number_id, text):
    to = FORWARD_NUMBER.replace("+", "")
    event_id = str(uuid.uuid4())[:8]
    log("info", "Preparando forward_text", {"to": to, "text": text}, event_id)
    resp = send_message(phone_number_id, to, {"text": {"body": text}})
    if not resp or resp.status_code != 200:
        log("error", "forward_text falhou", {"status": getattr(resp, "status_code", None)}, event_id)
        return False
    log("info", "forward_text: enviado com sucesso", {"to": to}, event_id)
    return True

def forward_media(phone_number_id, media_type, media_id, caption=None):
    if media_type not in ALLOWED_MEDIA_TYPES:
        log("warning", "Tipo de mídia não permitido", {"media_type": media_type})
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": FORWARD_NUMBER.replace("+", ""),
        "type": media_type,
        media_type: {"id": media_id}
    }
    if caption:
        payload[media_type]["caption"] = caption
    return send_message(phone_number_id, FORWARD_NUMBER.replace("+", ""), payload)

# ====================================
# HEALTH CHECK
# ====================================
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat(), "contacts": len(CONTACTS)}), 200

# ====================================
# WEBHOOK
# ====================================
@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        log("info", "Webhook verificado com sucesso")
        return challenge
    log("warning", "Falha na verificação do webhook", {"received_token": token})
    return "Erro de verificação", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    event_id = str(uuid.uuid4())[:8]
    raw = request.get_data(as_text=True)
    log("info", "Webhook recebido (raw payload)", {"payload": short_json(raw)}, event_id)

    try:
        data = request.get_json()
    except Exception as e:
        log("error", "Falha ao parsear JSON do webhook", {"error": str(e)}, event_id)
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

            if not phone_number_id or sender == FORWARD_NUMBER.replace("+", "") or msg_type in IGNORED_TYPES:
                continue

            norm_sender = re.sub(r'\D', '', sender or '')
            name = CONTACTS.get(norm_sender, CONTACTS.get('+' + norm_sender, msg.get("profile", {}).get("name", "") or "Desconhecido"))

            if name == "Desconhecido":
                log("info", "Contato não identificado no CSV", {
                    "sender": sender,
                    "norm_sender": norm_sender,
                    "csv_keys_sample": list(CONTACTS.keys())[:5]
                })

            text = ""
            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
            elif msg_type == "interactive":
                interactive = msg.get("interactive", {})
                if interactive.get("type") == "button":
                    text = interactive.get("button", {}).get("text", "")
                elif interactive.get("type") == "list_reply":
                    text = interactive.get("list_reply", {}).get("title", "")
            elif msg_type == "contacts":
                contacts = msg.get("contacts", [])
                text = " | ".join(f"{c.get('name', {}).get('formatted_name', '')} {c.get('phones', [{}])[0].get('phone', '')}" for c in contacts)

            reply = (
                f"Olá! Este número não está mais ativo.\n"
                f"Por favor, salve meu novo contato e me chame lá:\n"
                f"👉 https://wa.me/{NEW_NUMBER.replace('+', '') if NEW_NUMBER else ''}"
            )
            send_message(phone_number_id, sender, {"text": {"body": reply}})

            tz_brasilia = timezone(timedelta(hours=-3))
            hora_local = datetime.now(tz_brasilia).strftime("%H:%M:%S")
            formatted_phone = format_phone(sender)
            compact_text = (
                f"👤 {name}\n"
                f"📱 {formatted_phone}\n"
                f"🕓 {hora_local}\n"
                f"💬 {text or '(mensagem de mídia)'}"
            )

            forward_text(phone_number_id, compact_text)

            if msg_type in ALLOWED_MEDIA_TYPES:
                media_obj = msg.get(msg_type, {})
                media_id = media_obj.get("id")
                caption = media_obj.get("caption", "")
                if not forward_media(phone_number_id, msg_type, media_id, caption):
                    fetch_media_url_and_forward(phone_number_id, media_id, msg_type, caption)

    return "ok", 200

# ====================================
# STARTUP
# ====================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("info", "➡️ Aplicação iniciando", {"port": port})
    app.run(host="0.0.0.0", port=port)
