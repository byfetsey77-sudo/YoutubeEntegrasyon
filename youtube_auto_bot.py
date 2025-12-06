import os
import io
import requests
import json
import textwrap
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from google import genai
from google.genai.errors import APIError
from moviepy.editor import ImageClip, TextClip, CompositeVideoClip, AudioFileClip, ColorClip

# --- 1. AYARLAR VE API İSTEMCİLERİ ---

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") 

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    print("HATA: TELEGRAM_BOT_TOKEN ve GEMINI_API_KEY ortam değişkenlerinden okunmalıdır.")
    # Railway'de bu hata çıkmayacaktır.
    
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

# --- 3. VİDEO MONTAJ İŞLEVİ (Aşama 2: Kalite ve Alt Yazı) ---

def create_final_video(image_path, script_text, title):
    """Görseli alt yazılı 20 saniyelik videoya dönüştürür (MoviePy)."""
    
    # Çıktı dosya adını sadeleştirme
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '_'))[:30].strip().replace(' ', '_')
    output_path = f"video_{safe_title}.mp4"
    
    try:
        # 1. Görsel Klibi ve Ken Burns Efekti (Hafif Zoom)
        image_clip = ImageClip(image_path, duration=TEMP_DURATION)
        
        def zoom_in(t):
            # 1'den 1.1'e zoom (hafif hareket)
            scale = 1 + 0.1 * t / TEMP_DURATION 
            return image_clip.get_frame(t) * scale
        
        # Görüntüye zoom efektini uygula
        zoomed_clip = image_clip.fl(zoom_in, apply_to=['mask']).set_duration(TEMP_DURATION)
        
        # 2. Alt Yazı Kliplerini Oluşturma
        kelime_limit = 10 # Her alt yazı satırında max kelime sayısı
        tum_metin_parcalari = textwrap.wrap(script_text, kelime_limit)
        
        # Video süresini metin parçalarına eşit böl
        parca_suresi = TEMP_DURATION / len(tum_metin_parcalari) if tum_metin_parcalari else TEMP_DURATION

        final_clips = []
        current_time = 0

        for metin in tum_metin_parcalari:
            if not metin: continue
            
            # Alt yazı ayarları (Kalın font, gölgelendirme)
            txt_clip = TextClip(
                metin, 
                fontsize=50, 
                color='white', 
                stroke_color='#333333',
                stroke_width=2,
                font='Arial-Bold',
                size=(zoomed_clip.w * 0.9, None), # Ekran genişliğinin %90'ı
                align='center'
            ).set_position(('center', zoomed_clip.h * 0.8)).set_duration(parca_suresi)
            
            txt_clip = txt_clip.set_start(current_time)
            final_clips.append(txt_clip)
            current_time += parca_suresi

        # 3. Final Video Oluşturma (Görsel + Alt Yazılar)
        final_video = CompositeVideoClip([zoomed_clip] + final_clips)
        
        # 4. Videoyu Kaydetme
        final_video.write_videofile(
            output_path, 
            codec='libx264', 
            fps=24, 
            logger=None,
            temp_audiofile='temp-audio.m4a',
            remove_temp=True
        )
        return output_path
        
    except Exception as e:
        print(f"MoviePy video montajında hata: {e}")
        return None

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

        # AŞAMA 2: VİDEO MONTAJI VE ALT YAZI (MoviePy)
        await context.bot.send_message(chat_id=chat_id, text="🎬 Video montajı ve alt yazı ekleniyor (Lütfen bekleyiniz, bu 1-2 dakika sürebilir)...")
        
        temp_video_path = create_final_video(temp_image_path, script, youtube_title)

        if not temp_video_path:
            raise Exception("Video oluşturulamadı (MoviePy hatası).")

        # AŞAMA 3: TELEGRAM'A GÖNDERME
        await context.bot.send_message(chat_id=chat_id, text="✅ Video hazırlandı! Telegram üzerinden gönderiliyor...")
        
        with open(temp_video_path, 'rb') as video_file:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=f"🎥 **{youtube_title}**\n\nVideo otomatik olarak oluşturulmuştur. İndirip YouTube'a yükleyebilirsiniz.",
                parse_mode=telegram.constants.ParseMode.MARKDOWN
            )
        
    except APIError as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ API Hatası (Gemini): Anahtarınızı kontrol edin. Hata: {e}")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Genel İşlem Hatası: {e}")
        
    finally:
        # Temizlik
        cleanup_files(temp_image_path, temp_video_path)


# --- 5. ANA FONKSİYON VE BAŞLATMA ---

async def start_command(update, context):
    await update.message.reply_text(
        "Merhaba! Ben Otomatik YouTube İçerik Botuyum. Lütfen bir video fikri yazın. Örneğin: **Antik Mısır'daki kedilerin önemi**"
    )

async def handle_message(update, context):
    video_idea = update.message.text.strip()
    # Komut olmayan metinleri işleriz
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
