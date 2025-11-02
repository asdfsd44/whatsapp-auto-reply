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
IGNORED_TYPES = ["status"]

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
    """Formata número E.164 → 55 34 997216766 (sem traço)"""
    digits = "".join(ch for ch in num if ch.isdigit())
    if len(digits) < 10:
        return digits
    ddi = digits[:2]
    ddd = digits[2:4]
    base = digits[4:]
    return f"{ddi} {ddd} {base}"

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

# ====================================
# HEALTH CHECK
# ====================================
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()}), 200

# ====================================
# WEBHOOK PRINCIPAL
# ====================================
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Erro de verificação", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return "sem conteúdo", 200

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                if not messages:
                    continue

                msg = messages[0]
                sender = msg.get("from")
                phone_number_id = value["metadata"]["phone_number_id"]
                msg_type = msg.get("type", "text")

                # ignora loops e tipos indesejados
                if sender == FORWARD_NUMBER.replace("+", "") or msg_type in IGNORED_TYPES:
                    continue

                text = msg.get("text", {}).get("body", "") if msg_type == "text" else ""
                name = msg.get("profile", {}).get("name", "")

                # responde automaticamente
                reply = (
                    f"Olá! Este número não está mais ativo.\n"
                    f"Por favor, salve meu novo contato e me chame lá:\n"
                    f"👉 https://wa.me/{NEW_NUMBER.replace('+', '')}"
                )
                send_message(phone_number_id, sender, {"text": {"body": reply}})

                # registra remetente
                try:
                    with open(REMETENTES_FILE, "a", encoding="utf-8") as f:
                        f.write(f"{sender}\t{name}\t{datetime.utcnow().isoformat()}\n")
                except Exception as e:
                    log("error", "Erro ao registrar remetente", {"error": str(e)})

                # horário com fuso -03
                tz_brasilia = timezone(timedelta(hours=-3))
                hora_local = datetime.now(tz_brasilia).strftime("%H:%M:%S")

                formatted_phone = format_phone(sender)
                compact_text = (
                    f"👤 {name or 'Desconhecido'}\n"
                    f"📱 {formatted_phone}\n"
                    f"🕓 {hora_local}\n"
                    f"💬 {text or '(mensagem de mídia)'}"
                )

                forward_text(phone_number_id, compact_text)

                # se for mídia, encaminha também
                if msg_type in ALLOWED_MEDIA_TYPES:
                    media_id = msg[msg_type].get("id")
                    caption = msg[msg_type].get("caption", "")
                    forward_media(phone_number_id, msg_type, media_id, caption)

    except Exception as e:
        log("error", "Erro no processamento do webhook", {"error": str(e)})

    return "ok", 200

# ====================================
# ROTINA DE REENVIO (FILA)
# ====================================
def retry_worker():
    """Thread de retry persistente"""
    while True:
        try:
            queue = load_retries()
            new_queue = []
            for item in queue:
                if item.get("attempts", 0) >= MAX_RETRIES:
                    continue
                now = time.time()
                if now - item.get("last_try", 0) < RETRY_INTERVAL_SECONDS:
                    new_queue.append(item)
                    continue

                resp = send_message(item["phone_number_id"], item["to"], item["message"])
                if not resp or resp.status_code != 200:
                    item["attempts"] += 1
                    item["last_try"] = now
                    new_queue.append(item)
                    log("warning", "Reenvio falhou", {"to": item["to"], "attempt": item["attempts"]})
                else:
                    log("info", "Reenvio bem-sucedido", {"to": item["to"]})

            save_retries(new_queue)
        except Exception as e:
            log("error", "Erro na thread de retry", {"error": str(e)})

        time.sleep(RETRY_INTERVAL_SECONDS)

# inicia thread de retry
threading.Thread(target=retry_worker, daemon=True).start()

# ====================================
# EXECUÇÃO
# ====================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("info", f"🚀 Servidor rodando em http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)

