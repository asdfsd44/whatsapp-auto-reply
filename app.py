from flask import Flask, request
import requests
import os

app = Flask(__name__)

# Variáveis de ambiente (configuradas no Render)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
NEW_NUMBER = os.environ.get("NEW_NUMBER")
NEW_NAME = os.environ.get("NEW_NAME", "Novo Contato")

@app.route("/webhook", methods=["GET"])
def verify():
    """Verificação inicial exigida pelo Meta"""
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        return challenge
    return "Erro de verificação", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    """Recebe mensagens e responde automaticamente"""
    data = request.get_json()
    if not data:
        return "Sem conteúdo", 200

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                if not messages:
                    continue

                sender = messages[0]["from"]
                phone_number_id = value["metadata"]["phone_number_id"]

                # Envia mensagem de texto
                text_message = (
                    "Olá! Este número não está mais ativo. "
                    "Por favor, salve meu novo contato para continuar falando comigo.\n\n"
                    f"👉 https://wa.me/{NEW_NUMBER.replace('+', '')}"
                )

                send_message(phone_number_id, sender, {"text": {"body": text_message}})

                # Envia o contato (vCard)
                vcard_payload = {
                    "messaging_product": "whatsapp",
                    "to": sender,
                    "type": "contacts",
                    "contacts": [
                        {
                            "name": {"formatted_name": NEW_NAME},
                            "phones": [
                                {"phone": NEW_NUMBER, "type": "CELL"}
                            ],
                        }
                    ],
                }

                url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
                headers = {
                    "Authorization": f"Bearer {ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                }
                requests.post(url, json=vcard_payload, headers=headers)

                print(f"✔️ Mensagem e contato enviados para {sender}")

    except Exception as e:
        print(f"❌ Erro ao processar: {e}")

    return "OK", 200


def send_message(phone_number_id, to, message_content):
    """Função auxiliar para enviar mensagens"""
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
    }
    payload.update(message_content)
    requests.post(url, json=payload, headers=headers)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
