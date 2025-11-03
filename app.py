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
FORWARD_NUMBER = os.environ.get("FORWARD_NUMBER", "+5534997216766")
CONTACTS_URL = os.environ.get("CONTACTS_URL")

LOG_FILE = "app.log"
ALLOWED_MEDIA_TYPES = ["image", "document", "audio"]
IGNORED_TYPES = ["status", "sticker", "reaction", "location", "unknown", "video"]

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
        s = json.dumps(data, ensure_ascii=False) if data else ""
    except Exception:
        s = str(data)
    getattr(logging, level)(f"{message} | data={s}")

# ====================================
# CONTATOS
# ====================================
def normalize_number(raw):
    """
    Normaliza o número pegando apenas os 8 últimos dígitos,
    ignorando DDI e DDD.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits[-8:] if len(digits) >= 8 else digits

def load_contacts_from_drive():
    contacts = {}
    if not CONTACTS_URL:
        log("warning", "CONTACTS_URL ausente")
        return contacts

    try:
        log("info", "Baixando contatos do Google Drive", {"url": CONTACTS_URL})
        resp = requests.get(CONTACTS_URL, timeout=20)
        resp.raise_for_status()
        text = resp.content.decode("utf-8", errors="ignore")

        raw_lines = text.splitlines()
        total_lines = len(raw_lines)

        for line in raw_lines:
            phones = re.findall(r"\+?\d[\d\s\-\(\)]{8,}\d", line)
            if not phones:
                continue
            possible_names = re.findall(r"[A-ZÀ-Ÿ][a-zà-ÿ]+(?: [A-ZÀ-Ÿa-zà-ÿ]+)*", line)
            name = possible_names[0] if possible_names else "Desconhecido"
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
# FUNÇÕES AUXILIARES
# ====================================
def send_message(phone_number_id, to, message):
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to}
    payload.update(message)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        log("info", "Send message result", {"to": to, "status": resp.status_code})
        if resp.status_code != 200:
            log("warning", "Graph API response", {"status": resp.status_code, "response": resp.text})
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
# WEBHOOK
# ====================================
@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        log("info", "Webhook verificado")
        return challenge
    return "Erro de verificação", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
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

            # Normaliza o número pelo final (8 dígitos)
            norm_sender = normalize_number(sender)
            name = CONTACTS.get(norm_sender, "Desconhecido")

            if name == "Desconhecido":
                log("info", "Contato não identificado", {
                    "sender": sender,
                    "amostra": list(CONTACTS.keys())[:5]
                })
            else:
                log("info", "Contato identificado", {
                    "sender": sender,
                    "nome": name
                })

            text = ""
            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
            elif msg_type == "contacts":
                cts = msg.get("contacts", [])
                text = " | ".join(f"{c.get('name', {}).get('formatted_name', '')} {c.get('phones', [{}])[0].get('phone', '')}" for c in cts)

            # Resposta automática
            reply = (
                f"Olá! Este número não está mais ativo.\n"
                f"Por favor, salve meu novo contato e me chame lá:\n"
                f"👉 https://wa.me/{NEW_NUMBER.replace('+', '') if NEW_NUMBER else ''}"
            )
            send_message(phone_number_id, sender, {"text": {"body": reply}})

            # Encaminhamento
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
        "timestamp": datetime.utcnow().isoformat()
    })

# ====================================
# STARTUP
# ====================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("info", "➡️ Aplicação iniciando", {"port": port})
    app.run(host="0.0.0.0", port=port)
