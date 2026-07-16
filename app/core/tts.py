"""Optional text-to-speech via edge-tts (free Microsoft Edge neural voices).

The import is lazy so the app runs fine without the package installed; the
voice/video feature in the image tab is simply disabled until it is present.
Only the QThread worker at the bottom touches Qt.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal


class TTSError(Exception):
    pass


def tts_available() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


# (label hiển thị, edge-tts voice id)
VOICES = [
    # — Tiếng Việt —
    ("🇻🇳 Hoài My — nữ", "vi-VN-HoaiMyNeural"),
    ("🇻🇳 Nam Minh — nam", "vi-VN-NamMinhNeural"),
    # — Đa ngôn ngữ (đọc được cả tiếng Việt lẫn tiếng Anh) —
    ("🌐 Ava — nữ, đa ngôn ngữ", "en-US-AvaMultilingualNeural"),
    ("🌐 Emma — nữ, đa ngôn ngữ", "en-US-EmmaMultilingualNeural"),
    ("🌐 Andrew — nam, đa ngôn ngữ", "en-US-AndrewMultilingualNeural"),
    ("🌐 Brian — nam, đa ngôn ngữ", "en-US-BrianMultilingualNeural"),
    ("🌐 Vivienne — nữ, đa ngôn ngữ (Pháp)", "fr-FR-VivienneMultilingualNeural"),
    ("🌐 Remy — nam, đa ngôn ngữ (Pháp)", "fr-FR-RemyMultilingualNeural"),
    ("🌐 Seraphina — nữ, đa ngôn ngữ (Đức)", "de-DE-SeraphinaMultilingualNeural"),
    ("🌐 Florian — nam, đa ngôn ngữ (Đức)", "de-DE-FlorianMultilingualNeural"),
    # — Tiếng Anh (Mỹ) —
    ("🇺🇸 Aria — nữ, tự tin", "en-US-AriaNeural"),
    ("🇺🇸 Jenny — nữ, thân thiện", "en-US-JennyNeural"),
    ("🇺🇸 Michelle — nữ, dễ chịu", "en-US-MichelleNeural"),
    ("🇺🇸 Emma — nữ, vui tươi", "en-US-EmmaNeural"),
    ("🇺🇸 Ava — nữ, biểu cảm", "en-US-AvaNeural"),
    ("🇺🇸 Ana — nữ trẻ em", "en-US-AnaNeural"),
    ("🇺🇸 Guy — nam, sôi nổi", "en-US-GuyNeural"),
    ("🇺🇸 Andrew — nam, ấm áp", "en-US-AndrewNeural"),
    ("🇺🇸 Brian — nam, gần gũi", "en-US-BrianNeural"),
    ("🇺🇸 Christopher — nam, trầm chắc", "en-US-ChristopherNeural"),
    ("🇺🇸 Eric — nam, điềm đạm", "en-US-EricNeural"),
    ("🇺🇸 Roger — nam, hoạt bát", "en-US-RogerNeural"),
    ("🇺🇸 Steffan — nam, rành mạch", "en-US-SteffanNeural"),
    # — Tiếng Anh (Anh / Úc / Canada / Ireland / Ấn Độ) —
    ("🇬🇧 Sonia — nữ, UK", "en-GB-SoniaNeural"),
    ("🇬🇧 Libby — nữ, UK", "en-GB-LibbyNeural"),
    ("🇬🇧 Maisie — nữ trẻ em, UK", "en-GB-MaisieNeural"),
    ("🇬🇧 Ryan — nam, UK", "en-GB-RyanNeural"),
    ("🇬🇧 Thomas — nam, UK", "en-GB-ThomasNeural"),
    ("🇦🇺 Natasha — nữ, AU", "en-AU-NatashaNeural"),
    ("🇦🇺 William — nam, AU", "en-AU-WilliamMultilingualNeural"),
    ("🇨🇦 Clara — nữ, CA", "en-CA-ClaraNeural"),
    ("🇨🇦 Liam — nam, CA", "en-CA-LiamNeural"),
    ("🇮🇪 Emily — nữ, IE", "en-IE-EmilyNeural"),
    ("🇮🇳 Neerja — nữ, IN", "en-IN-NeerjaNeural"),
    # — Ngôn ngữ khác —
    ("🇯🇵 Nanami — nữ, Nhật", "ja-JP-NanamiNeural"),
    ("🇯🇵 Keita — nam, Nhật", "ja-JP-KeitaNeural"),
    ("🇰🇷 SunHi — nữ, Hàn", "ko-KR-SunHiNeural"),
    ("🇰🇷 InJoon — nam, Hàn", "ko-KR-InJoonNeural"),
    ("🇨🇳 Xiaoxiao — nữ, Trung", "zh-CN-XiaoxiaoNeural"),
    ("🇨🇳 Yunxi — nam, Trung", "zh-CN-YunxiNeural"),
    ("🇫🇷 Denise — nữ, Pháp", "fr-FR-DeniseNeural"),
    ("🇫🇷 Henri — nam, Pháp", "fr-FR-HenriNeural"),
    ("🇪🇸 Elvira — nữ, Tây Ban Nha", "es-ES-ElviraNeural"),
    ("🇲🇽 Dalia — nữ, Mexico", "es-MX-DaliaNeural"),
    ("🇩🇪 Katja — nữ, Đức", "de-DE-KatjaNeural"),
    ("🇮🇹 Isabella — nữ, Ý", "it-IT-IsabellaNeural"),
    ("🇵🇹 Thalita — nữ, Bồ Đào Nha (Brazil)", "pt-BR-ThalitaMultilingualNeural"),
    ("🇷🇺 Svetlana — nữ, Nga", "ru-RU-SvetlanaNeural"),
    ("🇹🇭 Premwadee — nữ, Thái", "th-TH-PremwadeeNeural"),
    ("🇮🇩 Gadis — nữ, Indonesia", "id-ID-GadisNeural"),
    ("🇮🇳 Swara — nữ, Hindi", "hi-IN-SwaraNeural"),
]

SAMPLE_TEXTS = {
    "vi": ("Xin chào, đây là giọng đọc thử nghiệm. Bạn đang nghe một câu mẫu "
           "khoảng năm mươi từ để cảm nhận tốc độ, ngữ điệu và độ tự nhiên "
           "của giọng đọc này. Nếu thấy phù hợp với video của mình, hãy chọn "
           "giọng này và bấm tạo voice để bắt đầu chuyển toàn bộ kịch bản "
           "thành âm thanh."),
    "en": ("Hello, this is a sample voice preview. You are listening to a "
           "short passage of about fifty words so you can judge the pace, "
           "intonation and natural feel of this voice. If it sounds right "
           "for your video, select it and press create voice to convert "
           "your whole script into audio."),
    "ja": ("こんにちは、これは音声プレビューです。この声の速さ、イントネーション、"
           "自然さを確認するための短いサンプル文章をお聞きいただいています。"
           "動画に合うと感じたら、この声を選んでください。"),
    "ko": ("안녕하세요, 이것은 음성 미리듣기입니다. 이 목소리의 속도와 억양, "
           "자연스러움을 확인할 수 있도록 짧은 샘플 문장을 들려드리고 있습니다. "
           "영상에 어울린다면 이 목소리를 선택해 주세요."),
    "zh": ("你好，这是语音试听。你正在收听一段简短的示例文字，"
           "以便感受这个声音的语速、语调和自然程度。"
           "如果觉得适合你的视频，请选择这个声音开始配音。"),
    "fr": ("Bonjour, ceci est un aperçu de la voix. Vous écoutez un court "
           "passage d'une cinquantaine de mots pour juger du rythme, de "
           "l'intonation et du naturel de cette voix. Si elle convient à "
           "votre vidéo, sélectionnez-la."),
    "es": ("Hola, esta es una vista previa de la voz. Estás escuchando un "
           "breve pasaje de unas cincuenta palabras para valorar el ritmo, "
           "la entonación y la naturalidad de esta voz. Si encaja con tu "
           "video, selecciónala."),
    "de": ("Hallo, dies ist eine Sprachvorschau. Sie hören einen kurzen "
           "Abschnitt von etwa fünfzig Wörtern, um Tempo, Betonung und "
           "Natürlichkeit dieser Stimme zu beurteilen. Wenn sie zu Ihrem "
           "Video passt, wählen Sie sie aus."),
    "it": ("Ciao, questa è un'anteprima della voce. Stai ascoltando un breve "
           "passaggio di circa cinquanta parole per valutare il ritmo, "
           "l'intonazione e la naturalezza di questa voce. Se è adatta al "
           "tuo video, selezionala."),
    "pt": ("Olá, esta é uma prévia da voz. Você está ouvindo um trecho curto "
           "de cerca de cinquenta palavras para avaliar o ritmo, a entonação "
           "e a naturalidade desta voz. Se combinar com o seu vídeo, "
           "selecione-a."),
    "ru": ("Здравствуйте, это предварительное прослушивание голоса. Вы "
           "слушаете короткий отрывок, чтобы оценить темп, интонацию и "
           "естественность этого голоса. Если он подходит для вашего видео, "
           "выберите его."),
    "th": ("สวัสดีค่ะ นี่คือตัวอย่างเสียงพากย์ คุณกำลังฟังข้อความสั้น ๆ "
           "เพื่อสัมผัสจังหวะ น้ำเสียง และความเป็นธรรมชาติของเสียงนี้ "
           "หากเหมาะกับวิดีโอของคุณ โปรดเลือกเสียงนี้"),
    "id": ("Halo, ini adalah pratinjau suara. Anda sedang mendengarkan "
           "cuplikan singkat sekitar lima puluh kata untuk menilai tempo, "
           "intonasi, dan kealamian suara ini. Jika cocok dengan video Anda, "
           "silakan pilih suara ini."),
    "hi": ("नमस्ते, यह एक आवाज़ का पूर्वावलोकन है। आप इस आवाज़ की गति, लहजा "
           "और स्वाभाविकता परखने के लिए एक छोटा सा अंश सुन रहे हैं। अगर यह "
           "आपके वीडियो के लिए सही लगे, तो इसे चुनें।"),
}


def sample_text_for(voice_id: str) -> str:
    return SAMPLE_TEXTS.get(voice_id.split("-", 1)[0].lower(), SAMPLE_TEXTS["en"])


def synthesize(text: str, voice: str, out_path: str, rate: str = "+0%",
               retries: int = 3,
               stop_event: threading.Event | None = None,
               ) -> list[tuple[float, float, str]]:
    """Blocking synthesis of ``text`` to an mp3 at ``out_path``.

    Returns the word boundaries as ``[(start_s, end_s, word), …]`` — used to
    time karaoke-style subtitles exactly to the speech. Call only from a
    worker thread; each call spins up its own asyncio loop. Raises TTSError
    on failure (after ``retries`` attempts).
    """
    import edge_tts

    async def _go() -> list[tuple[float, float, str]]:
        words: list[tuple[float, float, str]] = []
        try:  # edge-tts >= 7 defaults to SentenceBoundary
            com = edge_tts.Communicate(text, voice, rate=rate,
                                       boundary="WordBoundary")
        except TypeError:  # edge-tts 6.x: WordBoundary is the only mode
            com = edge_tts.Communicate(text, voice, rate=rate)
        with open(out_path, "wb") as f:
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # offsets/durations are in 100-ns ticks
                    start = chunk["offset"] / 1e7
                    end = start + chunk["duration"] / 1e7
                    words.append((start, end, str(chunk["text"])))
        return words

    last_err: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        if stop_event is not None and stop_event.is_set():
            raise TTSError("Đã dừng.")
        try:
            words = asyncio.run(_go())
            # edge-tts occasionally writes an empty file without raising.
            p = Path(out_path)
            if p.exists() and p.stat().st_size > 1024:
                return words
            last_err = TTSError("Không nhận được audio từ server.")
        except Exception as e:  # network hiccups, throttling, token refresh
            last_err = e
        if attempt < retries:
            if stop_event is not None:
                if stop_event.wait(2 * attempt):
                    raise TTSError("Đã dừng.")
            else:
                import time
                time.sleep(2 * attempt)
    raise TTSError(f"TTS thất bại sau {retries} lần thử: {last_err}")


class VoiceTestWorker(QThread):
    """One-shot: synthesize the ~50-word sample sentence to a temp mp3."""

    done = Signal(str)    # temp mp3 path
    failed = Signal(str)

    def __init__(self, voice: str, parent=None):
        super().__init__(parent)
        self.voice = voice

    def run(self) -> None:
        out = str(Path(tempfile.gettempdir()) / "cv_voice_test.mp3")
        try:
            synthesize(sample_text_for(self.voice), self.voice, out)
            self.done.emit(out)
        except Exception as e:
            self.failed.emit(str(e))
