from flask import Flask, request, jsonify
import requests
import os
import io
import json
import time
import threading
import logging
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

REMETENTES_FILE = "remetentes.txt"
RETRY_FILE = "retries.json"
LOG_FILE = "app.log"

ALLOWED_MEDIA_TYPES = ["image", "document", "audio"]
IGNORED_TYPES = ["status", "sticker", "reaction", "location", "unknown", "video"]

MAX_RETRIES = 5
RETRY_INTERVAL_SECONDS = 60  # intervalo entre tentativas

# ====================================
# LOGGING
# ====================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)

def log(level, message, data=None):
    msg = f"{message} | {json.dumps(data, ensure_ascii=False)}" if data else message
    getattr(logging, level)(msg)

# ====================================
# FUNÇÕES AUXILIARES
# ====================================
def load_retries():
    try:
        if os.path.exists(RETRY_FILE):
            with open(RETRY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_retries(queue):
    with open(RETRY_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

def format_phone(num: str) -> str:
    """Formata número E.164 → 55 34 98404-4040"""
    digits = "".join(ch for ch in num if ch.isdigit())
    if len(digits) < 11:
        return digits
    ddi = digits[:2]
    ddd = digits[2:4]
    middle = digits[4:9]
    end = digits[9:]
    return f"{ddi} {ddd} {middle}-{end}"

def send_message(phone_number_id, to, message):
    """Envia mensagem genérica via API do WhatsApp"""
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to}
    payload.update(message)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        return resp
    except Exception as e:
        log("error", "Erro ao enviar mensagem", {"error": str(e)})
        return None

def forward_text(phone_number_id, text):
    """Encaminha texto para o número principal"""
    return send_message(phone_number_id, FORWARD_NUMBER.replace("+", ""), {"text": {"body": text}})

def forward_media(phone_number_id, media_type, media_id, caption=None):
    """Encaminha imagem, documento ou áudio (exceto vídeo)"""
    try:
        if media_type not in ALLOWED_MEDIA_TYPES:
            return False
        url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp",
            "to": FORWARD_NUMBER.replace("+", ""),
            "type": media_type,
            media_type: {"id": media_id}
        }
        if caption:
            payload[media_type]["caption"] = caption
        r = requests.post(url, headers=headers, json=payload)
        return r.status_code == 200
    except Exception as e:
        log("error", "Erro ao encaminhar mídia", {"error": str(e)})
        return False

# =======
