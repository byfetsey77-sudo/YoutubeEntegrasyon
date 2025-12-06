import os
import io
import requests
import json
import textwrap
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from google import genai
from google.genai.errors import APIError
# moviepy importları geçici olarak devre dışı bırakıldı!
# from moviepy.editor import ImageClip, TextClip, CompositeVideoClip, AudioFileClip, ColorClip 

# --- 1. AYARLAR VE API İSTEMCİLERİ ---

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") 

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    print("HATA: TELEGRAM_BOT_TOKEN ve GEMINI_API_KEY ortam değişkenlerinden okunmalıdır.")
    
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    print(f"Gemini Client başlatma hatası: {e}")
    client = None

TEXT_MODEL = "gemini-2.5-flash" 
IMAGE_MODEL = "imagen-3.0-generate-002" 
TEMP_DURATION = 20 # Video süresi (saniye)

# --- 2. YARDIMCI İŞLEVLER ---

def download_image(image_url, save_path="temp_image.png"):
    """Gemini'dan gelen URL'deki görseli indirir."""
    response = requests.get(image_url)
    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            f.write(response.content)
        return save_path
    return None

def cleanup_files(*files):
    """İşlem bitince geçici dosyaları siler."""
    for f in files:
        if f and os.path.exists(f):
            os.remove(f)

# --- 3. VİDEO MONTAJ İŞLEVİ (GEÇİCİ YER TUTUCU) ---

def create_final_video(image_path, script_text, title):
    """MoviePy'den kaynaklanan hataları test etmek için geçici yer tutucu."""
    print("--- MoviePy Geçici Olarak Atlandı ---")
    # Video dosyası oluşturmuyoruz, sadece başarılı bir dosya yolu döndürüyoruz.
    return "temp_video_placeholder.mp4" 

# --- 4. TELEGRAM İŞLEYİCİSİ (ANA İŞ AKIŞI) ---

async def generate_and_process_video(update, context, video_idea):
    """Tüm süreci yöneten ana fonksiyon."""
    
    if not client:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ HATA: Gemini API Anahtarı eksik. Lütfen Railway'de 'GEMINI_API_KEY' değişkenini ayarlayın.")
        return
        
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text=f"🤖 Fikir alındı: '{video_idea}'. Başlıyorum...")

    temp_image_path, temp_video_path = None, None

    try:
        # AŞAMA 1: SENARYO VE GÖRSEL TALİMATI ÜRETİMİ (Gemini)
        await context.bot.send_message(chat_id=chat_id, text="📝 Senaryo ve görsel talimatları üretiliyor...")
        
        # JSON formatında çıktı isteme
        system_instruction = ("Tüm çıktılarını aşağıdaki formatta, SADECE JSON olarak ver. Ek metin EKLEME.")
        prompt = f"Video fikri: {video_idea}"
        
        response = client.chats.create(
            model=TEXT_MODEL,
            config={
                "systemInstruction": system_instruction, 
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT", "properties": {
                        "image_prompt": {"type": "STRING", "description": "Görsel üretim modeli için detaylı, İngilizce talimat."},
                        "script": {"type": "STRING", "description": f"{TEMP_DURATION} saniyelik Türkçe konuşma metni."},
                        "youtube_title": {"type": "STRING", "description": "YouTube videosu için ilgi çekici Türkçe başlık."}
                    }
                }
            }
        ).send_message(message=prompt)

        data = json.loads(response.text)
        image_prompt, script, youtube_title = data["image_prompt"], data["script"], data["youtube_title"]

        # AŞAMA 1.5: GÖRSEL ÜRETİMİ VE İNDİRME
        await context.bot.send_message(chat_id=chat_id, text="📸 Görsel oluşturuluyor ve indiriliyor...")
        
        image_result = client.models.generate_images(
            model=IMAGE_MODEL,
            prompt=image_prompt,
            config=dict(number_of_images=1, aspect_ratio="16:9")
        )
        
        image_url = image_result.generated_images[0].image.url
        temp_image_path = download_image(image_url)

        if not temp_image_path:
            raise Exception("Görsel indirme başarısız.")

        # AŞAMA 2: VİDEO MONTAJI (MoviePy yerine geçici fonksiyon çağrıldı)
        await context.bot.send_message(chat_id=chat_id, text="🎬 Video montajı adımı şimdilik atlanıyor...")
        
        temp_video_path = create_final_video(temp_image_path, script, youtube_title)

        # AŞAMA 3: TELEGRAM'A BİLDİRİM GÖNDERME
        await context.bot.send_message(chat_id=chat_id, text="✅ Video İçeriği Hazırlandı! Telegram üzerinden sonuç bildiriliyor...")
        
        # Sadece Metin Gönderme (Video Dosyası yerine)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎥 **{youtube_title}**\n\n**Senaryo:** {script[:150]}...\n\n✅ BOT BAŞARIYLA ÇALIŞIYOR. MoviePy kurulumunu çözdükten sonra video gelecektir!",
            parse_mode=telegram.constants.ParseMode.MARKDOWN
        )
        
    except APIError as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ API Hatası (Gemini): Anahtarınızı kontrol edin. Hata: {e}")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Genel İşlem Hatası: {e}")
        
    finally:
        # Temizlik (Sadece Görseli siliyoruz)
        cleanup_files(temp_image_path) # temp_video_path silinmez çünkü hiç oluşmadı.


# --- 5. ANA FONKSİYON VE BAŞLATMA ---

async def start_command(update, context):
    # Kullanıcı bilgisi hatılatması
    teacher_response = "Ben bir yapay zekayım." 

    await update.message.reply_text(
        f"Merhaba! Öğretmenin sorarsa: {teacher_response}\n\nBen Otomatik YouTube İçerik Botuyum. Lütfen bir video fikri yazın."
    )

async def handle_message(update, context):
    video_idea = update.message.text.strip()
    if video_idea.startswith('/'):
        return 
        
    await generate_and_process_video(update, context, video_idea)


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("HATA: TELEGRAM_BOT_TOKEN ortam değişkeni tanımlı değil.")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("YouTube Otomasyon Botu çalışmaya başladı...")
    app.run_polling(poll_interval=3)

if __name__ == '__main__':
    main()
