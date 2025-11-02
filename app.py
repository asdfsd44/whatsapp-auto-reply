                # encaminha para Luiz (mensagem compacta e formatada)
                from datetime import timezone, timedelta

                def format_phone(num: str) -> str:
                    """Formata número E.164 para exibição legível: 5534984044040 → 55 34 98404-4040"""
                    digits = "".join(ch for ch in num if ch.isdigit())
                    if len(digits) < 11:
                        return digits
                    ddi = digits[:2]
                    ddd = digits[2:4]
                    middle = digits[4:9]
                    end = digits[9:]
                    return f"{ddi} {ddd} {middle}-{end}"

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
