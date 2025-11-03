import os
import re
import csv
import json
import requests
import logging
from datetime import datetime
from flask import Flask, request

# ---------------------------------------------
# LOGGING CONFIGURATION
# ---------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s | data=%(message)s"
)
app = Flask(__name__)

# ---------------------------------------------
# CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ---------------------------------------------
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "tokenpadrao")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
FORWARD_NUMBER = os.environ.get("FORWARD_NUMBER", "")
CONTACTS_URL = os.environ.get("CONTACTS_URL", "")
TEMPLATE_NAME = os.environ.get("TEMPLATE_NAME", "forward_alert")

CONTACTS = {}

# ---------------------------------------------
# FUNÇÃO: NORMALIZA LINK DO GOOGLE DRIVE
# ---------------------------------------------
def normalize_drive_url(url: str) -> str:
    """Converte automaticamente link de visualização do Google Drive para link direto."""
    pattern = r"https://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)/view"
    match = re.search(pattern, url)
    if match:
        file_id = match.group(1)
        fixed = f"https://drive.google.com/uc?export=download&id={file_id}"
        logging.info(f"URL do Drive convertida automaticamente | data={{'original': '{url}', 'corrigida': '{fixed}'}}")
        return fixed
    return url

# ---------------------------------------------
# FUNÇÃO: BAIXAR E CARREGAR CONTATOS
# ---------------------------------------------
def load_contacts():
    global CONTACTS
    if not CONTACTS_URL:
        logging.warning("Variável CONTACTS_URL não definida.")
        return

    try:
        url = normalize_drive_url(CONTACTS_URL)
        logging.info(f"Baixando contatos do Google Drive | data={{'url': '{url}'}}")
        response = requests.get(url)
        response.raise_for_status()
        lines = response.text.strip().splitlines()
        reader = csv.DictReader(lines)

        total_linhas = 0
        contatos_temp = {}
        for row in reader:
            total_linhas += 1
            nome = (row.get("First Name") or "").strip()
            telefone = (row.get("Phone1Value") or "").strip()
            telefone_norm = re.sub(r"\D", "", telefone)
            if telefone_norm.startswith("0"):
                telefone_norm = telefone_norm[1:]
            if telefone_norm.startswith("55"):
                telefone_norm = telefone_norm
            elif len(telefone_norm) >= 10:
                telefone_norm = f"55{telefone_norm}"
            if len(telefone_norm) >= 11:
                contatos_temp[telefone_norm] = nome

        CONTACTS = contatos_temp
        logging.info(
            f"Contatos carregados e normalizados | data={{'total_linhas': {total_linhas}, 'total_contatos': {len(CONTACTS)}}}"
        )
    except Exception as e:
        logging.error(f"Erro ao carregar contatos | data={{'erro': '{str(e)}'}}")

# ---------------------------------------------
# FUNÇÃO: NORMALIZAR NÚMEROS
# ---------------------------------------------
def normalize_number(num: str) -> str:
    norm = re.sub(r"\D", "", num or "")
    if norm.startswith("0"):
        norm = norm[1:]
    if not norm.startswith("55") and len(norm) >= 10:
        norm = f"55{norm}"
    return norm

# ---------------------------------------------
# FUNÇÃO: ENVIAR MENSAGEM VIA WHATSAPP
# ---------------------------------------------
# ---------------------------------------------
# FUNÇÃO: ENVIAR MENSAGEM VIA WHATSAPP (CORRIGIDA)
# ---------------------------------------------
def send_message(to, text):
    try:
        # Remove caracteres indesejados
        to = str(to).replace("+", "").strip()

        url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "text": {"body": text}
        }

        r = requests.post(url, headers=headers, json=payload)

        # Log de status resumido
        logging.info(f"Send message result | data={{'to': '{to}', 'status': {r.status_code}}}")

        # Log detalhado para depuração (temporário)
        if r.status_code != 200:
            logging.warning(f"Graph API response | data={{'status': {r.status_code}, 'response': {r.text}}}")
        else:
            logging.info(f"Graph API OK | data={{'message_id': {r.json().get('messages', [{}])[0].get('id', 'unknown')}}}")

    except Exception as e:
        logging.error(f"Erro ao enviar mensagem | data={{'erro': '{str(e)}'}}")

    try:
        url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "text": {"body": text}
        }
        r = requests.post(url, headers=headers, json=payload)
        logging.info(f"Send message result | data={{'to': '{to}', 'status': {r.status_code}}}")
    except Exception as e:
        logging.error(f"Erro ao enviar mensagem | data={{'erro': '{str(e)}'}}")

# ---------------------------------------------
# ROTA: WEBHOOK DE VERIFICAÇÃO
# ---------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# ---------------------------------------------
# ROTA: WEBHOOK RECEBIMENTO DE MENSAGENS
# ---------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.data.decode("utf-8")
    try:
        logging.info(f"Webhook recebido (raw payload) | data={{'payload': {json.dumps(data)}}}")
        body = json.loads(data)
        entry = body.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return "OK", 200

        msg = messages[0]
        sender = msg.get("from")
        text = msg.get("text", {}).get("body", "")
        timestamp = datetime.now().strftime("%H:%M:%S")

        norm_sender = normalize_number(sender)
        nome = CONTACTS.get(norm_sender)

        if nome:
            logging.info(f"Contato identificado | data={{'sender': '{norm_sender}', 'nome': '{nome}'}}")
            nome_exibicao = nome
        else:
            logging.info(f"Contato não identificado | data={{'sender': '{norm_sender}', 'amostra': {list(CONTACTS.keys())[:5]}}}")
            nome_exibicao = "Desconhecido"

        # Resposta automática
        auto_reply = (
            "Olá! Este número não está mais ativo.\n"
            "Por favor, salve meu novo contato e me chame lá:\n"
            "👉 https://wa.me/5534997216766"
        )
        send_message(sender, auto_reply)

        # Encaminha para o forward
        forward_text = (
            f"👤 {nome_exibicao}\n"
            f"📱 {norm_sender[:2]} {norm_sender[2:]} \n"
            f"🕓 {timestamp}\n"
            f"💬 {text}"
        )
        send_message(FORWARD_NUMBER, forward_text)

    except Exception as e:
        logging.error(f"Erro no webhook | data={{'erro': '{str(e)}'}}")

    return "OK", 200

# ---------------------------------------------
# ROTA PRINCIPAL
# ---------------------------------------------
@app.route("/", methods=["GET"])
def home():
    return "Webhook WhatsApp ativo.", 404

# ---------------------------------------------
# MAIN
# ---------------------------------------------
if __name__ == "__main__":
    load_contacts()
    app.run(host="0.0.0.0", port=10000)
else:
    load_contacts()

