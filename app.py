"""
WhatsApp Auto Reply Bot — Sessão Meta 24h + Lembrete Automático

Autor: Luiz Becker
Função: Gerenciar autorespostas WhatsApp, encaminhamento de mensagens,
        controle de sessão Meta (24h) e lembrete automático.
"""

from flask import Flask, request, jsonify
import requests
import os
import json
import time
import threading
import logging
import re
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# ==========================================================
# CONFIGURAÇÕES DE AMBIENTE
# ==========================================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")  # Token para verificação do webhook
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")  # Token de acesso Graph API
NEW_NUMBER = os.environ.get("NEW_NUMBER")  # Novo número de contato
FORWARD_NUMBER = os.environ.get("FORWARD_NUMBER")  # Para onde encaminhar mensagens
CONTACTS_URL = os.environ.get("CONTACTS_URL")  # URL pública CSV dos contatos
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")  # ID do número WhatsApp Business

REMINDER_HOURS_BEFORE = int(os.environ.get("REMINDER_HOURS_BEFORE", "1"))
REMINDER_TO = os.environ.get("REMINDER_TO", FORWARD_NUMBER)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
USE_LAST8_MATCH = os.environ.get("USE_LAST8_MATCH", "true").lower() in ("1", "true", "yes")

# ==========================================================
# LOGGING
# ==========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

def log(level, message, data=None):
    """Registra logs em formato JSON estruturado."""
    try:
        s = json.dumps(data, ensure_ascii=False) if data else ""
    except Exception:
        s = str(data)
    getattr(logging, level)(f"{message} | data={s}")

# ==========================================================
# CARREGAMENTO DE CONTATOS
# ==========================================================
def normalize_number(raw):
    """Remove caracteres não numéricos e garante prefixo +55."""
    raw = re.sub(r"\D", "", raw or "")
    if not raw:
        return None
    if raw.startswith("55") and len(raw) > 13:
        raw = raw[-13:]
    if not raw.startswith("55"):
        raw = "55" + raw[-11:]
    return raw

def load_contacts_from_drive():
    """Lê CSV do Google Drive e constrói dicionário {telefone: nome}."""
    contacts = {}
    if not CONTACTS_URL:
        log("warning", "CONTACTS_URL ausente")
        return contacts
    try:
        log("info", "Baixando contatos do Google Drive", {"url": CONTACTS_URL})
        resp = requests.get(CONTACTS_URL, timeout=30)
        resp.raise_for_status()
        text = resp.content.decode("utf-8", errors="ignore")

        for line in text.splitlines():
            phones = re.findall(r"\+?\d[\d\s\-\(\)]{6,}\d", line)
            if not phones:
                continue
            possible_names = re.findall(r"[A-Za-zÀ-ÿ0-9\-\.\&\s]{1,60}", line)
            name = next((p.strip() for p in possible_names if not re.search(r"\d", p.strip())), "Desconhecido")
            for phone in phones:
                n = normalize_number(phone)
                if n:
                    contacts[n] = name
        log("info", "Contatos carregados", {"total": len(contacts)})
    except Exception as e:
        log("error", "Erro ao processar contatos", {"error": str(e)})
    return contacts

CONTACTS = load_contacts_from_drive()

# ==========================================================
# ENVIO DE MENSAGENS
# ==========================================================
def send_message(phone_number_id, to, message):
    """Envia mensagem de texto via Graph API (v20)."""
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to}
    payload.update(message)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        log("info", "Send message result", {"to": to, "status": resp.status_code})
        return resp
    except Exception as e:
        log("error", "Erro ao enviar", {"error": str(e)})
        return None

def format_phone(num):
    """Formata número para exibição (XX XX XXXXXXXX)."""
    digits = "".join(ch for ch in (num or "") if ch.isdigit())
    return f"{digits[:2]} {digits[2:4]} {digits[4:]}" if len(digits) >= 10 else digits

# ==========================================================
# CONTROLE DE SESSÃO META (24H)
# ==========================================================
LAST_ACTIVITY = datetime.utcnow()
LAST_REMINDER_SENT = None

def update_activity():
    """Atualiza a última atividade e reinicia controle de lembrete."""
    global LAST_ACTIVITY, LAST_REMINDER_SENT
    LAST_ACTIVITY = datetime.utcnow()
    LAST_REMINDER_SENT = None

def check_meta_session():
    """
    Thread que verifica inatividade e dispara lembrete antes do vencimento da janela Meta 24h.
    Executa continuamente em background a cada CHECK_INTERVAL_SECONDS.
    """
    global LAST_ACTIVITY, LAST_REMINDER_SENT
    while True:
        try:
            now = datetime.utcnow()
            delta = (now - LAST_ACTIVITY).total_seconds()
            threshold = (24 - REMINDER_HOURS_BEFORE) * 3600
            if delta >= threshold and not LAST_REMINDER_SENT:
                if PHONE_NUMBER_ID:
                    to = REMINDER_TO.replace("+", "")
                    msg = {"text": {"body": "⚠️ Atenção — a janela de 24h da API está próxima de expirar. Envie qualquer mensagem para renovar a sessão."}}
                    resp = send_message(PHONE_NUMBER_ID, to, msg)
                    log("warning", "Lembrete de sessão enviado", {"to": to, "status": getattr(resp, "status_code", None)})
                else:
                    log("warning", "PHONE_NUMBER_ID não definido, lembrete não enviado.")
                LAST_REMINDER_SENT = now.isoformat()
        except Exception as e:
            log("error", "check_meta_session exception", {"error": str(e)})
        time.sleep(CHECK_INTERVAL_SECONDS)

threading.Thread(target=check_meta_session, daemon=True).start()

# ==========================================================
# WEBHOOK HANDLER
# ==========================================================
@app.route("/webhook", methods=["GET"])
def verify():
    """Verifica token do webhook Meta."""
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        log("info", "Webhook verificado")
        return challenge
    return "Erro de verificação", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    """Recebe mensagens, responde e encaminha."""
    try:
        payload = request.get_json()
        sender_id = payload.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [{}])[0].get("from")
        if sender_id and sender_id.endswith("97216766"):
            update_activity()
    except Exception:
        pass

    raw = request.get_data(as_text=True)
    log("info", "Webhook recebido", {"payload": raw[:150]})

    data = request.get_json(silent=True)
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
            if msg_type in ["status", "reaction", "sticker", "unknown"]:
                continue

            norm_sender = re.sub(r"\D", "", sender or "")
            name = CONTACTS.get(norm_sender)

            if not name and USE_LAST8_MATCH:
                last8 = norm_sender[-8:]
                for k, v in CONTACTS.items():
                    if k.endswith(last8):
                        name = v
                        break
            if not name:
                name = "Desconhecido"

            text = msg.get("text", {}).get("body", "") if msg_type == "text" else "(mensagem de mídia)"
            reply = f"Olá! Este número não está mais ativo.\nPor favor, salve meu novo contato:\n👉 https://wa.me/{NEW_NUMBER.replace('+', '') if NEW_NUMBER else ''}"
            send_message(phone_number_id, sender, {"text": {"body": reply}})

            hora = datetime.now(timezone(timedelta(hours=-3))).strftime("%H:%M:%S")
            forward = f"👤 {name}\n📱 {format_phone(sender)}\n🕓 {hora}\n💬 {text}"
            send_message(phone_number_id, FORWARD_NUMBER.replace("+", ""), {"text": {"body": forward}})

    return "ok", 200

# ==========================================================
# ENDPOINT DE TESTE DE LEMBRETE
# ==========================================================
@app.route("/force_reminder", methods=["GET"])
def force_reminder():
    """Força envio imediato de lembrete de sessão."""
    token = request.args.get("token")
    if token != VERIFY_TOKEN:
        return jsonify({"error": "Acesso negado"}), 403
    if not PHONE_NUMBER_ID:
        return jsonify({"error": "PHONE_NUMBER_ID não configurado"}), 400

    to = REMINDER_TO.replace("+", "")
    msg = {"text": {"body": "⚠️ Teste manual: simulação de expiração Meta 24h."}}
    resp = send_message(PHONE_NUMBER_ID, to, msg)
    log("warning", "Lembrete forçado manualmente", {"to": to, "status": getattr(resp, "status_code", None)})
    return jsonify({"result": "Lembrete enviado manualmente", "status_code": getattr(resp, "status_code", None)})

# ==========================================================
# HEALTH CHECK
# ==========================================================
@app.route("/health")
def health_check():
    """Retorna status e métricas básicas."""
    return jsonify({
        "status": "ok",
        "total_contacts": len(CONTACTS),
        "last_activity_utc": LAST_ACTIVITY.isoformat()
    })

# ==========================================================
# MAIN
# ==========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("info", "➡️ Aplicação iniciando", {"port": port})
    app.run(host="0.0.0.0", port=port)
