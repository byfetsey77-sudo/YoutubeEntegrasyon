import os
import io
import requests
import json
import textwrap
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from google import genai
from google.genai.errors import APIError
from moviepy.editor import ImageClip, TextClip, CompositeVideoClip, AudioFileClip # Video montajı için
# Not: moviepy kurulumunda ek olarak FFMPEG gereklidir. Railway bunu otomatik halleder.

# --- 1. ORTAM DEĞİŞKENLERİNİ OKU ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") 
# YOUTUBE_API_KEY ve diğerleri şu an kullanılmayacak, sadece tanımlı.

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    # Konsola uyarı basar, ancak Railway'de bu değişkenler tanımlı olacağı için sorun olmaz.
    print("UYARI: API Anahtarları ortam değişkenlerinden okunacak.")

# Gemini ve Model Tanımları
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
except:
    client = None # Anahtar yoksa client'ı None olarak ayarlarız.

TEXT_MODEL = "gemini-2.5-flash" 
IMAGE_MODEL = "imagen-3.0-generate-002" 
TEMP_DURATION = 20 # Saniye

# --- 2. VİDEO MONTAJ İŞLEVİ (Aşama 2) ---

def create_final_video(image_path, script_text, title):
    """Görseli alt yazılı videoya dönüştürür."""
    # Çıktı dosya adını Telegram'a göndermek için sadeleştiriyoruz
    safe_title = title[:20].replace(' ', '_').replace('.', '').replace('/', '')
    output_path = f"video_{safe_title}.mp4"
    
    try:
        image_clip = ImageClip(image_path, duration=TEMP_DURATION)
        
        # Ken Burns Efekti (Hafif Zoom)
        def zoom_in(t):
            scale = 1 + 0.1 * t / TEMP_DURATION
            return image_clip.get_frame(t) * scale
        
        zoomed_clip = image_clip.fl(zoom_in, apply_to=['mask']).set_duration(TEMP_DURATION)
        
        # Senaryoyu Alt Yazı Kliplerine Bölme
        kelime_limit = 10 
        tum_metin_parcalari = textwrap.wrap(script_text, kelime_limit)
        parca_suresi = TEMP_DURATION / len(tum_metin_parcalari)

        final_clips = []
        current_time = 0

        for metin in tum_metin_parcalari:
            if not metin: continue
            
            txt_clip = TextClip(
                metin, 
                fontsize=50, 
                color='white', 
                stroke_color='#333333',
                stroke_width=2,
                font='Arial-Bold',
                size=(zoomed_clip.w * 0.9, None),
                align='center'
            ).set_position(('center', zoomed_clip.h * 0.8)).set_duration(parca_suresi)
            
            txt_clip = txt_clip.set_start(current_time)
            final_clips.append(txt_clip)
            current_time += parca_suresi

        final_video = CompositeVideoClip([zoomed_clip] + final_clips)
        
        # Videoyu kaydetme
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

# --- 3. TELEGRAM VE GEMINI İŞLEVLERİ ---

def download_image(image_url, save_path="temp_image.png"):
    """Gemini'dan gelen URL'deki görseli indirir."""
    response = requests.get(image_url)
    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            f.write(response.content)
        return save_path
    return None

async def generate_and_process_video(update, context, video_idea):
    """Tüm süreci yöneten ana fonksiyon."""
    if not client:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ HATA: Gemini API Anahtarı eksik. Lütfen Railway'de 'GEMINI_API_KEY' değişkenini ayarlayın.")
        return
        
    chat_id = update.effective_chat.id
    
    await context.bot.send_message(chat_id=chat_id, text=f"🤖 Fikir alındı: '{video_idea}'. Senaryo ve görsel talimatları üretiliyor...")

    temp_image_path = None
    temp_video_path = None

    try:
        # AŞAMA 1: METİN ÜRETİMİ (Senaryo, Başlık, Görsel İstek Metni)
        system_instruction = ("Tüm çıktılarını aşağıdaki formatta, SADECE JSON olarak ver. Ek metin EKLEME.")
        prompt = f"Video fikri: {video_idea}"
        
        response = client.chats.create(
            model=TEXT_MODEL,
            config={"systemInstruction": system_instruction, "responseMimeType": "application/json",
                "responseSchema": {"type": "OBJECT", "properties": {
                    "image_prompt": {"type": "STRING", "description": "Görsel üretim modeli için detaylı, İngilizce talimat."},
                    "script": {"type": "STRING", "description": f"{TEMP_DURATION} saniyelik Türkçe konuşma metni."},
                    "youtube_title": {"type": "STRING", "description": "YouTube videosu için ilgi çekici Türkçe başlık."}}}
            }
        ).send_message(message=prompt)

        data = json.loads(response.text)
        image_prompt, script, youtube_title = data["image_prompt"], data["script"], data["youtube_title"]

        await context.bot.send_message(chat_id=chat_id, text=f"📸 Şimdi görseli oluşturuyorum...")
        
        # AŞAMA 1.5: GÖRSEL ÜRETİMİ
        image_result = client.models.generate_images(
            model=IMAGE_MODEL,
            prompt=image_prompt,
            config=dict(number_of_images=1, aspect_ratio="16:9")
        )
        
        image_url = image_result.generated_images[0].image.url
        temp_image_path = download_image(image_url)

        if not temp_image_path:
            await context.bot.send_message(chat_id=chat_id, text="❌ Hata: Üretilen görseli indiremedim.")
            return

        # AŞAMA 2: VİDEO MONTAJI VE ALT YAZI
        await context.bot.send_message(chat_id=chat_id, text="🎬 Video montajı ve alt yazı ekleniyor (Bu biraz zaman alabilir)...")
        
        temp_video_path = create_final_video(temp_image_path, script, youtube_title)

        if not temp_video_path:
            await context.bot.send_message(chat_id=chat_id, text="❌ Hata: Video oluşturulamadı.")
            return

        # AŞAMA 3: TELEGRAM'A GÖNDERME
        await context.bot.send_message(chat_id=chat_id, text="✅ Video hazırlandı! Şimdi Telegram üzerinden gönderiliyor...")
        
        # Videoyu Telegram'a gönder
        with open(temp_video_path, 'rb') as video_file:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=f"🎥 **{youtube_title}**\n\nVideo otomatik olarak oluşturulmuştur. İndirip YouTube'a yükleyebilirsiniz.",
                parse_mode=telegram.constants.ParseMode.MARKDOWN
            )
        
    except Exception as e:
        print(f"Genel İşlem Hatası: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Proje hatası oluştu: {e}")
        
    finally:
        # Temizlik: Oluşturulan dosyaları sil
        if temp_image_path and os.path.exists(temp_image_path): os.remove(temp_image_path)
        if temp_video_path and os.path.exists(temp_video_path): os.remove(temp_video_path)


async def start_command(update, context):
    await update.message.reply_text(
        "Merhaba! Ben Otomatik YouTube İçerik Botuyum. Lütfen bir video fikri yazın."
    )

async def handle_message(update, context):
    video_idea = update.message.text.strip()
    await generate_and_process_video(update, context, video_idea)


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("HATA: TELEGRAM_BOT_TOKEN tanımlı değil.")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("YouTube Otomasyon Botu çalışmaya başladı...")
    app.run_polling(poll_interval=3)

if __name__ == '__main__':
    main()