from flask import Flask, request
import requests
import os
import io

app = Flask(__name__)

# Variáveis de ambiente
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
NEW_NUMBER = os.environ.get("NEW_NUMBER")
NEW_NAME = os.environ.get("NEW_NAME", "Novo Contato")

REMETENTES_FILE = "remetentes.txt"

@app.route("/webhook", methods=["GET"])
def verify():
    """Verificação inicial do Meta"""
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        print("✔️ Verificação de webhook bem-sucedida")
        return challenge
    print("❌ Falha na verificação do webhook")
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

                print(f"📩 Nova mensagem recebida de {sender}")

                # Salva o remetente no arquivo
                with open(REMETENTES_FILE, "a") as f:
                    f.write(f"{sender}\n")

                # 1️⃣ Envia mensagem de texto
                text_message = (
                    "Olá! Este número não está mais ativo. "
                    "Por favor, salve meu novo contato.\n\n"
                    f"👉 https://wa.me/{NEW_NUMBER.replace('+', '')}"
                )

                resp_text = send_message(phone_number_id, sender, {"text": {"body": text_message}})
                print(f"✉️ Texto enviado para {sender} → Status: {resp_text.status_code} / {resp_text.text}")

                # 2️⃣ Envia o novo contato real em formato .VCF
                send_vcard(phone_number_id, sender)

    except Exception as e:
        print(f"❌ Erro ao processar webhook: {e}")

    return "OK", 200


def send_message(phone_number_id, to, message_content):
    """Função auxiliar para enviar mensagens de texto"""
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
    response = requests.post(url, json=payload, headers=headers)
    return response


def send_vcard(phone_number_id, to):
    """Gera e envia o vCard real (.vcf) diretamente pela API"""
    try:
        # 1️⃣ Conteúdo do vCard
        vcard_content = f"""BEGIN:VCARD
VERSION:3.0
N:{NEW_NAME};Contato;;;
FN:{NEW_NAME}
ORG:{NEW_NAME}
TEL;type=CELL;waid={NEW_NUMBER.replace('+', '')}:{NEW_NUMBER}
END:VCARD
"""

        # 2️⃣ Gera arquivo em memória
        arquivo_vcf = io.BytesIO(vcard_content.encode("utf-8"))

        # 3️⃣ Upload do arquivo para a Meta (media endpoint)
        upload_url = f"https://graph.facebook.com/v20.0/{phone_number_id}/media"
        files = {
            'file': ('contato.vcf', arquivo_vcf, 'text/vcard'),
        }
        data = {'messaging_product': 'whatsapp'}
        headers = {'Authorization': f'Bearer {ACCESS_TOKEN}'}

        upload_response = requests.post(upload_url, headers=headers, files=files, data=data)
        upload_result = upload_response.json()
        print(f"📤 Upload do vCard → {upload_response.status_code}: {upload_result}")

        if 'id' not in upload_result:
            print("❌ Falha ao subir o vCard:", upload_result)
            return

        media_id = upload_result['id']

        # 4️⃣ Envia a mensagem de documento com o vCard
        message_url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "document",
            "document": {
                "id": media_id,
                "filename": f"{NEW_NAME}.vcf"
            }
        }

        headers = {
            'Authorization': f'Bearer {ACCESS_TOKEN}',
            'Content-Type': 'application/json'
        }

        send_response = requests.post(message_url, headers=headers, json=payload)
        print(f"📇 vCard enviado para {to} → {send_response.status_code} / {send_response.text}")

    except Exception as e:
        print(f"❌ Erro ao enviar vCard: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor rodando em http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
