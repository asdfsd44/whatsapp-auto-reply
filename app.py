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
MAX_LOG_FIELD = 2000  # quando o texto for maior que isso, truncar nos logs

# ====================================
# LOGGING (arquivo + console)
# ====================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)

def log(level: str, message: str, data: dict = None, event_id: str = None):
    """Log unificado com event_id e truncamento inteligente."""
    prefix = f"[{event_id}] " if event_id else ""
    details = ""
    if data is not None:
        try:
            s = json.dumps(data, ensure_ascii=False, indent=None)
        except Exception:
            s = str(data)
        if len(s) > MAX_LOG_FIELD:
            s = s[:MAX_LOG_FIELD] + "...(truncated)"
        details = f" | data={s}"
    final = prefix + message + details
    getattr(logging, level)(final)

# ====================================
# UTILITÁRIOS
# ====================================
def now_iso():
    return datetime.utcnow().isoformat()

def load_retries():
    try:
        if os.path.exists(RETRY_FILE):
            with open(RETRY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log("error", "Erro ao carregar retries.json", {"error": str(e)})
    return []

def save_retries(queue):
    try:
        with open(RETRY_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        log("info", "Fila de retry salva", {"count": len(queue)})
    except Exception as e:
        log("error", "Erro ao salvar retries.json", {"error": str(e)})

def enqueue_retry(item):
    """Adiciona item (dict) na fila persistente de retry."""
    queue = load_retries()
    queue.append(item)
    save_retries(queue)
    log("warning", "Item enfileirado para retry", {"to": item.get("to"), "attempts": item.get("attempts", 0)})

def format_phone(num: str) -> str:
    """Formata número E.164 → 55 34 997216766 (sem traço)"""
    digits = "".join(ch for ch in (num or "") if ch.isdigit())
    if len(digits) < 10:
        return digits
    ddi = digits[:2]
    ddd = digits[2:4]
    base = digits[4:]
    return f"{ddi} {ddd} {base}"

def short_json(obj, max_len=MAX_LOG_FIELD):
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return s if len(s) <= max_len else s[:max_len] + "...(truncated)"

# ====================================
# SEND / FORWARD helpers (com logs ricos)
# ====================================
def send_message(phone_number_id, to, message):
    """Envia mensagem via Graph API com logs detalhados."""
    event_id = str(uuid.uuid4())[:8]
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to}
    payload.update(message)

    log("info", "Tentando enviar mensagem", {"url": url, "to": to, "payload": payload}, event_id)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        status = resp.status_code
        text = resp.text
        log("info", "Resposta do Graph API", {"status_code": status, "response_text": text}, event_id)

        if status != 200:
            # enfileira para retry automático
            enqueue_retry({
                "id": event_id,
                "phone_number_id": phone_number_id,
                "to": to,
                "message": message,
                "attempts": 1,
                "last_try": time.time()
            })
            log("error", "Falha no envio, item colocado em retry", {"status_code": status, "response_text": text}, event_id)
        return resp
    except Exception as e:
        log("error", "Exceção ao postar para Graph API", {"error": str(e)}, event_id)
        # enfileira também
        enqueue_retry({
            "id": event_id,
            "phone_number_id": phone_number_id,
            "to": to,
            "message": message,
            "attempts": 1,
            "last_try": time.time()
        })
        return None

def forward_text(phone_number_id, text):
    """Encaminha texto para FORWARD_NUMBER com logs"""
    to = FORWARD_NUMBER.replace("+", "")
    event_id = str(uuid.uuid4())[:8]
    log("info", "Preparando forward_text", {"to": to, "text": text}, event_id)
    resp = send_message(phone_number_id, to, {"text": {"body": text}})
    if resp is None:
        log("error", "forward_text: resposta nula ao enviar", {"to": to}, event_id)
        return False
    if resp.status_code != 200:
        log("error", "forward_text: envio retornou erro", {"status_code": resp.status_code, "response": resp.text}, event_id)
        return False
    log("info", "forward_text: enviado com sucesso", {"to": to}, event_id)
    return True

def forward_media(phone_number_id, media_type, media_id, caption=None):
    """Encaminha imagem/documento/áudio para FORWARD_NUMBER via media id."""
    event_id = str(uuid.uuid4())[:8]
    if media_type not in ALLOWED_MEDIA_TYPES:
        log("warning", "forward_media: tipo de mídia não permitido", {"media_type": media_type}, event_id)
        return False

    to = FORWARD_NUMBER.replace("+", "")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": media_type,
        media_type: {"id": media_id}
    }
    if caption:
        payload[media_type]["caption"] = caption

    # log antes de enviar
    log("info", "forward_media: tentando encaminhar mídia", {"to": to, "media_id": media_id, "media_type": media_type, "caption": caption}, event_id)

    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        log("info", "forward_media: resposta do Graph API", {"status_code": resp.status_code, "response_text": resp.text}, event_id)
        if resp.status_code != 200:
            enqueue_retry({
                "id": event_id,
                "phone_number_id": phone_number_id,
                "to": to,
                "message": payload,
                "attempts": 1,
                "last_try": time.time()
            })
            log("error", "forward_media: falha no envio, enfileirado", {"status_code": resp.status_code}, event_id)
            return False
        log("info", "forward_media: enviado com sucesso", {"to": to}, event_id)
        return True
    except Exception as e:
        log("error", "forward_media: exceção ao enviar mídia", {"error": str(e)}, event_id)
        enqueue_retry({
            "id": event_id,
            "phone_number_id": phone_number_id,
            "to": to,
            "message": payload,
            "attempts": 1,
            "last_try": time.time()
        })
        return False

# ====================================
# MEDIA helper (download original media url)
# ====================================
def fetch_media_url_and_forward(phone_number_id, media_id, media_type, caption=None):
    """Baixa URL da API e tenta encaminhar usando media_id (quando possível)."""
    event_id = str(uuid.uuid4())[:8]
    try:
        # 1) Pega meta da mídia (contém url)
        meta_url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        log("info", "fetch_media: solicitando meta da mídia", {"meta_url": meta_url}, event_id)
        meta_resp = requests.get(meta_url, headers=headers, timeout=15)
        try:
            meta_json = meta_resp.json()
        except Exception:
            log("error", "fetch_media: meta_resp não é JSON", {"status_code": meta_resp.status_code, "text": meta_resp.text}, event_id)
            return False

        media_url = meta_json.get("url")
        if not media_url:
            log("error", "fetch_media: url não encontrada na meta", {"meta": short_json(meta_json)}, event_id)
            return False

        # 2) Baixa conteúdo da URL (protegida) – passar o mesmo header
        log("info", "fetch_media: baixando conteúdo da URL", {"media_url": media_url}, event_id)
        content_resp = requests.get(media_url, headers=headers, timeout=30)
        if content_resp.status_code != 200:
            log("error", "fetch_media: falha ao baixar conteúdo", {"status_code": content_resp.status_code}, event_id)
            return False

        # 3) Faz upload para Graph /media para obter novo media_id (da conta do app)
        upload_url = f"https://graph.facebook.com/v20.0/{phone_number_id}/media"
        files = {'file': ('file', content_resp.content)}  # let API deduzir mime
        data = {'messaging_product': 'whatsapp'}
        upload_resp = requests.post(upload_url, headers=headers, files=files, data=data, timeout=30)
        try:
            upload_json = upload_resp.json()
        except Exception:
            log("error", "fetch_media: upload_resp não é JSON", {"status_code": upload_resp.status_code, "text": upload_resp.text}, event_id)
            return False

        new_media_id = upload_json.get("id")
        log("info", "fetch_media: upload realizado", {"new_media_id": new_media_id, "upload_resp": short_json(upload_json)}, event_id)
        if not new_media_id:
            log("error", "fetch_media: novo media_id não retornado", {"upload_resp": short_json(upload_json)}, event_id)
            return False

        # 4) Encaminha com novo media_id
        result = forward_media(phone_number_id, media_type, new_media_id, caption)
        return result

    except Exception as e:
        log("error", "fetch_media: exceção geral", {"error": str(e)}, event_id)
        return False

# ====================================
# HEALTH CHECK
# ====================================
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()}), 200

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
        log("warning", "Webhook vazio", None, event_id)
        return "ok", 200

    log("info", "Processando webhook entries", {"entries": len(data.get("entry", []))}, event_id)

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                if not messages:
                    log("info", "Nenhuma mensagem neste change", {"change": short_json(change)}, event_id)
                    continue

                msg = messages[0]
                sender = msg.get("from")
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                msg_type = msg.get("type", "text")

                log("info", "Mensagem recebida", {"sender": sender, "phone_number_id": phone_number_id, "type": msg_type}, event_id)

                # segurança básica: precisa phone_number_id
                if not phone_number_id:
                    log("error", "phone_number_id ausente no payload", {"change": short_json(change)}, event_id)
                    continue

                # evita loop: ignore mensagens enviadas pelo own FORWARD_NUMBER
                if sender == FORWARD_NUMBER.replace("+", ""):
                    log("info", "Mensagem originada do forward number — ignorando para evitar loop", {"sender": sender}, event_id)
                    continue

                # ignora tipos indesejados
                if msg_type in IGNORED_TYPES:
                    log("info", "Tipo de mensagem ignorado", {"type": msg_type}, event_id)
                    continue

                # salva remetente
                try:
                    with open(REMETENTES_FILE, "a", encoding="utf-8") as f:
                        f.write(f"{sender}\t{msg.get('profile', {}).get('name','')}\t{now_iso()}\n")
                    log("info", "Remetente registrado", {"sender": sender}, event_id)
                except Exception as e:
                    log("error", "Erro ao escrever remetentes.txt", {"error": str(e)}, event_id)

                # extrai dados
                name = msg.get("profile", {}).get("name", "")
                text = ""
                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "")
                elif msg_type == "interactive":
                    # botão/lista
                    interactive = msg.get("interactive", {})
                    itype = interactive.get("type")
                    if itype == "button":
                        text = interactive.get("button", {}).get("text") or interactive.get("button", {}).get("payload", "")
                    elif itype == "list_reply":
                        text = interactive.get("list_reply", {}).get("title") or interactive.get("list_reply", {}).get("id", "")
                elif msg_type == "contacts":
                    # contacts: montar texto e também encaminhar vCard (recriado)
                    contacts = msg.get("contacts", [])
                    contact_texts = []
                    for c in contacts:
                        fname = c.get("name", {}).get("formatted_name", "")
                        phones = c.get("phones", [])
                        ph = phones[0].get("phone") if phones else ""
                        contact_texts.append(f"{fname} {ph}")
                    text = " | ".join(contact_texts) if contact_texts else ""
                else:
                    text = short_json(msg)

                # responde ao remetente com mensagem padrão
                reply = (
                    f"Olá! Este número não está mais ativo.\n"
                    f"Por favor, salve meu novo contato e me chame lá:\n"
                    f"👉 https://wa.me/{NEW_NUMBER.replace('+', '') if NEW_NUMBER else ''}"
                )
                send_resp = send_message(phone_number_id, sender, {"text": {"body": reply}})
                if send_resp is None:
                    log("error", "Resposta ao remetente falhou (send_response is None)", {"sender": sender}, event_id)
                else:
                    log("info", "Resposta ao remetente enviada", {"status_code": send_resp.status_code, "response": send_resp.text}, event_id)

                # preparar texto compacto para encaminhar
                tz_brasilia = timezone(timedelta(hours=-3))
                hora_local = datetime.now(tz_brasilia).strftime("%H:%M:%S")
                formatted_phone = format_phone(sender)
                compact_text = (
                    f"👤 {name or 'Desconhecido'}\n"
                    f"📱 {formatted_phone}\n"
                    f"🕓 {hora_local}\n"
                    f"💬 {text or '(mensagem de mídia)'}"
                )

                # encaminha texto compactado
                forwarded_ok = forward_text(phone_number_id, compact_text)
                if not forwarded_ok:
                    log("error", "Falha ao encaminhar texto compacto", {"to": FORWARD_NUMBER, "sender": sender}, event_id)

                # se for mídia (image/document/audio): obter media id e tentar encaminhar
                if msg_type in ALLOWED_MEDIA_TYPES:
                    media_obj = msg.get(msg_type, {})
                    media_id = media_obj.get("id")
                    caption = media_obj.get("caption", "")
                    if not media_id:
                        log("warning", "Mídia sem media_id no payload", {"msg": short_json(msg)}, event_id)
                    else:
                        # tenta encaminhar diretamente usando media_id (padrão)
                        ok = forward_media(phone_number_id, msg_type, media_id, caption)
                        if not ok:
                            # fallback: baixar a mídia e re-upload/forward
                            log("info", "forward_media falhou, tentando fetch+upload", {"media_id": media_id}, event_id)
                            ok2 = fetch_media_url_and_forward(phone_number_id, media_id, msg_type, caption)
                            if not ok2:
                                log("error", "Falha ao encaminhar mídia mesmo após fetch+upload", {"media_id": media_id}, event_id)

                # se for contato, recriar .vcf e enviar como documento (opcional)
                if msg_type == "contacts":
                    contacts = msg.get("contacts", [])
                    for c in contacts:
                        nome_contato = c.get("name", {}).get("formatted_name", "Contato sem nome")
                        telefones = c.get("phones", [])
                        numero_contato = telefones[0].get("phone") if telefones else ""
                        vcard_content = f"""BEGIN:VCARD
VERSION:3.0
FN:{nome_contato}
TEL;type=CELL:{numero_contato}
END:VCARD
"""
                        try:
                            arquivo_vcf = io.BytesIO(vcard_content.encode("utf-8"))
                            upload_url = f"https://graph.facebook.com/v20.0/{phone_number_id}/media"
                            headers = {'Authorization': f'Bearer {ACCESS_TOKEN}'}
                            files = {'file': (f"{nome_contato}.vcf", arquivo_vcf, 'text/plain')}
                            data = {'messaging_product': 'whatsapp'}
                            upload_resp = requests.post(upload_url, headers=headers, files=files, data=data, timeout=30)
                            log("info", "contacts: upload do vCard realizado", {"status_code": upload_resp.status_code, "response": upload_resp.text}, event_id)
                            up_json = {}
                            try:
                                up_json = upload_resp.json()
                            except Exception:
                                pass
                            media_id = up_json.get("id")
                            if media_id:
                                # enviar como documento ao FORWARD_NUMBER
                                doc_payload = {
                                    "type": "document",
                                    "document": {"id": media_id, "filename": f"{nome_contato}.vcf", "caption": f"Contato: {nome_contato}"}
                                }
                                resp = send_message(phone_number_id, FORWARD_NUMBER.replace("+", ""), doc_payload)
                                if resp is None or resp.status_code != 200:
                                    log("error", "contacts: falha ao enviar vCard para FORWARD_NUMBER", {"status": getattr(resp, "status_code", None), "text": getattr(resp, "text", None)}, event_id)
                                else:
                                    log("info", "contacts: vCard enviado com sucesso para FORWARD_NUMBER", {"to": FORWARD_NUMBER}, event_id)
                            else:
                                log("warning", "contacts: upload não retornou media_id", {"upload_resp": short_json(up_json)}, event_id)
                        except Exception as e:
                            log("error", "contacts: exceção ao processar contato", {"error": str(e), "contact": c}, event_id)

    except Exception as e:
        log("error", "Erro geral ao processar webhook", {"error": str(e), "incoming": short_json(data)}, event_id)

    return "ok", 200

# ====================================
# THREAD DE RETRY
# ====================================
def retry_worker():
    while True:
        try:
            queue = load_retries()
            if not queue:
                time.sleep(RETRY_INTERVAL_SECONDS)
                continue

            log("info", "Iniciando ciclo de retry", {"pending": len(queue)})
            new_queue = []
            for item in queue:
                attempts = item.get("attempts", 0)
                if attempts >= MAX_RETRIES:
                    log("warning", "Descartando item após max attempts", {"id": item.get("id"), "attempts": attempts})
                    continue
                last_try = item.get("last_try", 0)
                now_ts = time.time()
                if now_ts - last_try < RETRY_INTERVAL_SECONDS:
                    new_queue.append(item)
                    continue

                # tentar reenviar
                phone_number_id = item.get("phone_number_id")
                to = item.get("to")
                message = item.get("message")
                log("info", "Tentando reenvio de item", {"id": item.get("id"), "to": to, "attempts": attempts})
                try:
                    resp = requests.post(f"https://graph.facebook.com/v20.0/{phone_number_id}/messages",
                                         headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
                                         json=message, timeout=20)
                    if resp.status_code == 200:
                        log("info", "Retry bem-sucedido", {"id": item.get("id"), "to": to})
                    else:
                        item["attempts"] = attempts + 1
                        item["last_try"] = now_ts
                        new_queue.append(item)
                        log("warning", "Retry falhou, permanecerá na fila", {"id": item.get("id"), "status": resp.status_code, "response": resp.text})
                except Exception as e:
                    item["attempts"] = attempts + 1
                    item["last_try"] = now_ts
                    new_queue.append(item)
                    log("error", "Retry: exceção ao reenviar item", {"id": item.get("id"), "error": str(e)})

            save_retries(new_queue)
        except Exception as e:
            log("error", "Thread retry: exceção não esperada", {"error": str(e)})
        time.sleep(RETRY_INTERVAL_SECONDS)

threading.Thread(target=retry_worker, daemon=True).start()

# ====================================
# STARTUP
# ====================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("info", "➡️ Aplicação iniciando", {"port": port})
    app.run(host="0.0.0.0", port=port)
