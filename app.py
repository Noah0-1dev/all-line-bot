import os
import logging
import re
import time
from threading import Lock

import httpx
from dotenv import load_dotenv
from flask import Flask, request, abort, render_template
from openai import OpenAI

# ==========================================
# 1. 載入環境設定
# ==========================================

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unibridge")

app = Flask(__name__)
PORT = int(os.getenv("PORT", "3000"))

CHANNEL_ACCESS_TOKEN = (
    os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    or os.getenv("CHANNEL_ACCESS_TOKEN")
)
CHANNEL_SECRET = (
    os.getenv("LINE_CHANNEL_SECRET")
    or os.getenv("CHANNEL_SECRET")
)

# Dify：主要 AI + RAG 校規知識庫
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = os.getenv(
    "DIFY_API_URL", "https://api.dify.ai/v1"
).rstrip("/")

# Gemini：Dify 故障時的備用 AI（預設使用 gemini-2.5-flash）
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/"
)

# 校園圖資設定（文藻校園平面圖）
CAMPUS_MAP_URL = "https://c005.wzu.edu.tw/datas/upload/files/Pictures/%E6%96%87%E8%97%BB%E5%A4%96%E8%AA%9E%E5%A4%A7%E5%AD%B8%E6%A0%A1%E5%9C%92%E5%B9%B3%E9%9D%A2%E5%9C%961029.jpg"

# 註：以下關鍵字清單中的越南語、印尼語、日語、韓語詞彙
# 是用來比對使用者輸入的，必須保留原語言，否則機器人會聽不懂該語言。
# 地圖關鍵字刻意只保留意思明確的詞，避免 "where"、"在哪裡" 等
# 一般疑問詞誤觸發地圖。
MAP_KEYWORDS = [
    # 繁體中文
    "地圖", "校園地圖", "平面圖",
    # English
    "map", "campus map",
    # Tiếng Việt
    "bản đồ", "ban do", "sơ đồ", "so do",
    # Bahasa Indonesia
    "peta",
    # 日本語
    "マップ", "地図",
    # 한국어
    "지도",
]

GREETING_KEYWORDS = [
    "hi", "hello", "hey", "chào", "xin chào", "halo", "こんにちは", "안녕하세요", "👋"
]

# 免責聲明（Disclaimer）
DISCLAIMER_TEXT = (
    "\n\n-------------------------\n"
    "💡 [提示]：本回覆由 AI 檢索學校規章生成，僅供參考，"
    "正式規定請以學校官網最新公告為準。"
)

# 校園公告模組：缺少時不影響一般 AI 功能
try:
    import campus_news
except Exception:
    campus_news = None
    logger.exception("無法載入 campus_news.py")

NEWS_ENABLED = (
    os.getenv("NEWS_ENABLED", "1") != "0"
    and campus_news is not None
)

# 觸發校園公告資料的關鍵字清單（支援中、英、越、印、日、韓）
NEWS_KEYWORDS = [
    # --- 1. 繁體中文 ---
    "公告", "最新", "消息", "新聞", "校園動態", "通知",
    "獎學金", "居留證", "arc", "工作證", "請假", "選課",
    "校曆", "行事曆", "宿舍", "工讀", "開戶", "學雜費",

    # --- 2. English ---
    "news", "announcement", "notice", "latest", "update",
    "scholarship", "arc", "visa", "work permit", "leave",
    "course", "registration", "calendar", "dormitory", "dorm",
    "tuition", "part-time", "bank account",

    # --- 3. Tiếng Việt ---
    "thông báo", "tin tức", "mới nhất", "cập nhật",
    "học bổng", "hoc bong", "gia hạn arc", "thẻ cư trú",
    "giấy phép làm việc", "xin nghỉ học", "nghỉ học", "đăng ký môn",
    "lịch học", "ký túc xá", "làm thêm", "mở tài khoản", "học phí",

    # --- 4. Bahasa Indonesia ---
    "pengumuman", "berita", "terbaru", "informasi",
    "beasiswa", "izin kerja", "cuti", "ijin",
    "krs", "pendaftaran matakuliah", "kalender akademik",
    "asrama", "kerja paruh waktu", "buka rekening", "biaya kuliah",

    # --- 5. 日本語（同時收錄日文新字體與繁體寫法）---
    "お知らせ", "最新情報", "ニュース", "案内",
    "奨学金", "獎學金", "在留カード", "居留証", "居留證",
    "就労許可証", "就勞許可証", "公欠", "休講",
    "履修登録", "学期日程", "寮", "アルバイト", "口座開設",
    "学費", "學費",

    # --- 6. 한국어 ---
    "공지", "공지사항", "최신", "뉴스", "소식",
    "장학금", "거류증", "아르바이트", "알바", "취업허가서",
    "휴學", "수강신청", "학사일정", "기숙사", "계좌개설", "등록금",
]


def _is_cjk(s):
    """判斷字串是否含中日韓文字（這類語言沒有空格分詞，不能用字邊界比對）。"""
    return any(
        0x2E80 <= ord(c) <= 0x9FFF
        or 0x3040 <= ord(c) <= 0x30FF
        or 0xAC00 <= ord(c) <= 0xD7AF
        for c in s
    )


def build_matcher(keywords):
    """建立關鍵字比對函式：拉丁字母 / 越南語使用字邊界，CJK 使用子字串。"""
    parts = []
    for k in keywords:
        k = k.lower()
        if _is_cjk(k):
            parts.append(re.escape(k))
        else:
            parts.append(rf"(?<!\w){re.escape(k)}(?!\w)")
    rx = re.compile("|".join(parts), re.IGNORECASE)
    return lambda text: bool(rx.search(text))


is_news = build_matcher(NEWS_KEYWORDS)
is_map = build_matcher(MAP_KEYWORDS)

if not all([CHANNEL_ACCESS_TOKEN, CHANNEL_SECRET]):
    raise RuntimeError(
        "缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET，"
        "請檢查 .env。"
    )

if not (DIFY_API_KEY or GEMINI_API_KEY):
    raise RuntimeError(
        "請至少設定 DIFY_API_KEY 或 GEMINI_API_KEY。"
    )

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    StickerMessageContent,
    ImageMessageContent,
    FollowEvent
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    ImageMessage,
    QuickReply,
    QuickReplyItem,
    MessageAction,
)

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# Gemini client（SDK 內建重試關閉，改由下方自訂迴圈處理 503）
ai_client = None
if GEMINI_API_KEY:
    ai_client = OpenAI(
        api_key=GEMINI_API_KEY,
        base_url=GEMINI_BASE_URL,
        timeout=20.0,
        max_retries=0,
    )

logger.info(
    "AI 後端：%s",
    "Dify（RAG，Gemini 備援）" if DIFY_API_KEY and ai_client
    else "Dify（RAG）" if DIFY_API_KEY else "Gemini"
)

# ==========================================
# 2. 校園公告與對話記錄整合
# ==========================================

def get_news_prompt():
    """供 Gemini 使用的校園公告提示詞。"""
    if not NEWS_ENABLED:
        return ""
    try:
        return campus_news.prompt_block() or ""
    except Exception:
        logger.exception("讀取校園公告提示詞失敗")
        return ""


def get_news_context():
    """取得校園公告，供 Dify 使用。"""
    if not NEWS_ENABLED:
        return ""
    try:
        max_chars = int(os.getenv("DIFY_NEWS_MAX_CHARS", "2000"))
        max_chars = max(500, min(max_chars, 6000))
        return campus_news.get_news_context(max_chars=max_chars) or ""
    except Exception:
        logger.exception("取得校園公告失敗")
        return ""


if NEWS_ENABLED:
    try:
        campus_news.start_background_refresh()
        logger.info("校園公告功能：已啟用")
    except Exception:
        logger.exception("啟動校園公告更新失敗")
else:
    logger.info("校園公告功能：未啟用")

chat_histories = {}
dify_conversations = {}
history_lock = Lock()
MAX_HISTORY_MESSAGES = 10

# SYSTEM_PROMPT（必須附上參考連結）
SYSTEM_PROMPT = """
你是 UniBridge，一位協助國際學生的 AI 小幫手，
服務對象為台灣文藻外語大學（Wenzao）的國際學生。

語言：
- 支援越南語、印尼語、日語、英文與繁體中文。
- 使用者使用哪種語言，就使用哪種語言回答。
- 使用中文時，一律使用繁體中文。
- 提到官方名稱或專有名詞時，附上中文原文（例如：居留證 ARC）。

任務：
- 協助學生處理 ARC（居留證）、工作證、請假、獎學金、
  開戶與匯款、校曆、校內工讀、選課、宿舍、
  生活、學習以及融入台灣生活等問題。
- 回答自然、親切、具體且實用。

[重要 - 參考連結與明確性]：
- 若知識庫（Knowledge Base）或公告中有提供學校官方網頁連結（URL），請務必在回答末尾附上該原始連結，方便學生點擊核對。
- 嚴禁僅回答「請洽 OIA」，必須列出具體步驟、金額、必備文件與相關網址。

正確性：
- 不可捏造規定、期限或官方流程。
- 校園公告若有提供，優先參考公告內容。
- 不可自稱是學校職員。

格式：
- 優先提供簡潔、具體的步驟。
- 嚴禁使用 Markdown 粗體語法 `**`。
- 若需要強調關鍵字，請一律使用方括號 `[...]` 代替（例如：[重要通知]）。
"""


def clean_markdown_formatting(text: str) -> str:
    """清理與替換 Markdown ** 語法，防止 LINE 顯示問題"""
    if not text:
        return ""
    text = re.sub(r'\*\*(.*?)\*\*', r'[\1]', text)
    text = text.replace("**", "")
    return text

# ==========================================
# 3. 快速回覆選單（Quick Reply Menu - 專屬第 6 格）
# ==========================================

def create_quick_reply():
    """僅包含 Rich Menu 5 個主要按鈕之外的擴充服務項目"""
    return QuickReply(
        items=[
            QuickReplyItem(
                action=MessageAction(
                    label="最新公告",
                    text="學校最近有什麼最新公告？"
                )
            ),
            QuickReplyItem(
                action=MessageAction(
                    label="請假流程",
                    text="學生線上請假流程是什麼？"
                )
            ),
            QuickReplyItem(
                action=MessageAction(
                    label="開戶與匯款",
                    text="留學生如何在台灣開戶與接收匯款？"
                )
            ),
            QuickReplyItem(
                action=MessageAction(
                    label="校園工讀",
                    text="如何申請校內工讀機會？"
                )
            ),
            QuickReplyItem(
                action=MessageAction(
                    label="選課流程",
                    text="請介紹選課流程"
                )
            ),
        ]
    )

# ==========================================
# 4. AI 邏輯（Dify & Gemini）
# ==========================================

def _dify_request(user_id, user_text, conversation_id, inputs=None):
    payload = {
        "inputs": inputs or {},
        "query": user_text,
        "response_mode": "blocking",
        "conversation_id": conversation_id,
        "user": user_id,
    }
    return httpx.post(
        f"{DIFY_API_URL}/chat-messages",
        headers={"Authorization": f"Bearer {DIFY_API_KEY}"},
        json=payload,
        timeout=25.0,
    )


def ask_dify(user_id, user_text):
    with history_lock:
        conversation_id = dify_conversations.get(user_id, "")
    dify_inputs = {}
    if is_news(user_text):
        news_text = get_news_context()
        if news_text:
            dify_inputs["campus_news"] = news_text

    resp = _dify_request(user_id, user_text, conversation_id, dify_inputs)
    if resp.status_code == 404 and conversation_id:
        with history_lock:
            dify_conversations.pop(user_id, None)
        conversation_id = ""
        resp = _dify_request(user_id, user_text, conversation_id, dify_inputs)

    if resp.status_code == 400 and dify_inputs:
        resp = _dify_request(user_id, user_text, conversation_id)

    if resp.status_code != 200:
        logger.error("Dify 回應錯誤 %s：%s", resp.status_code, resp.text[:500])
        resp.raise_for_status()

    data = resp.json()
    new_conversation_id = data.get("conversation_id")
    if new_conversation_id:
        with history_lock:
            dify_conversations[user_id] = new_conversation_id

    answer = (data.get("answer") or "").strip()
    if not answer:
        raise RuntimeError("Dify 回傳空白答案。")
    logger.info("Dify 已成功回覆使用者。")
    return clean_markdown_formatting(answer)


def ask_gemini(user_id, user_text):
    if ai_client is None:
        raise RuntimeError("Gemini 尚未設定，請檢查 GEMINI_API_KEY。")
    with history_lock:
        history = list(chat_histories.get(user_id, []))
    messages = history + [{"role": "user", "content": user_text}]
    system_content = SYSTEM_PROMPT
    if is_news(user_text):
        system_content += "\n" + get_news_prompt()

    max_retries = 3
    last_exception = None
    for attempt in range(max_retries):
        try:
            response = ai_client.chat.completions.create(
                model=GEMINI_MODEL,
                messages=[{"role": "system", "content": system_content}] + messages,
                max_tokens=1500,
            )
            answer = (response.choices[0].message.content or "").strip()
            if not answer:
                raise RuntimeError("Gemini 回傳空白答案。")
            updated_history = (messages + [{"role": "assistant", "content": answer}])[-MAX_HISTORY_MESSAGES:]
            if updated_history and updated_history[0]["role"] == "assistant":
                updated_history = updated_history[1:]
            with history_lock:
                chat_histories[user_id] = updated_history
            logger.info("Gemini 已成功回覆使用者。")
            return clean_markdown_formatting(answer)
        except Exception as e:
            last_exception = e
            if "503" in str(e) and attempt < max_retries - 1:
                logger.warning("Gemini 503 高負載，進行第 %d 次重試...", attempt + 1)
                time.sleep(1.5)
            else:
                break
    raise last_exception


def ask_ai(user_id, user_text):
    if DIFY_API_KEY:
        try:
            return ask_dify(user_id, user_text)
        except Exception:
            logger.exception("Dify 發生錯誤，準備切換 Gemini 備援。")
            if ai_client is None:
                raise RuntimeError("Dify 發生錯誤，而且未設定 Gemini 備援。")
            return ask_gemini(user_id, user_text)
    return ask_gemini(user_id, user_text)

# ==========================================
# 5. SERVER RUN & WEBHOOK
# ==========================================

@app.route("/", methods=["GET"])
def home():
    try:
        return render_template("index.html")
    except Exception:
        return "UniBridge AI Bot is running.", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        abort(400)
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("LINE 簽章驗證失敗。")
        abort(400)
    return "OK", 200

# ==========================================
# 6. EVENT HANDLERS
# ==========================================

def _reply(reply_token, messages):
    """統一的 LINE 回覆函式。"""
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


# --- 6.1 處理使用者加好友事件（FollowEvent）---
@handler.add(FollowEvent)
def handle_follow(event):
    welcome_text = (
        "[UniBridge - 文藻國際學生 AI 小幫手]\n"
        "👋 您好！我是協助文藻外語大學學生的 AI 小幫手。\n"
        "您可以點選下方選單，或直接輸入問題詢問我喔！"
    )
    try:
        _reply(event.reply_token, [TextMessage(text=welcome_text)])
        logger.info("已成功傳送加好友歡迎訊息。")
    except Exception:
        logger.exception("傳送加好友歡迎訊息失敗")

# --- 6.2 處理貼圖（Sticker）---
@handler.add(MessageEvent, message=StickerMessageContent)
def handle_sticker_message(event):
    reply_text = "👋 您好！今天有什麼可以幫您的嗎？請點選下方選單，或直接輸入問題喔！"
    try:
        _reply(event.reply_token, [TextMessage(text=reply_text)])
        logger.info("已成功處理 Sticker 訊息。")
    except Exception:
        logger.exception("傳送 Sticker 回覆失敗")

# --- 6.3 處理圖片 ---
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    reply_text = "[UniBridge] 目前還無法分析圖片，請改用文字輸入您的問題，謝謝！💬"
    try:
        _reply(event.reply_token, [TextMessage(text=reply_text)])
        logger.info("已成功處理 Image 訊息。")
    except Exception:
        logger.exception("傳送 Image 回覆失敗")

# --- 6.4 處理文字訊息 ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    # 略過 LINE 重送的事件，避免重複回覆
    delivery_context = getattr(event, "delivery_context", None)
    if getattr(delivery_context, "is_redelivery", False):
        logger.info("略過重送事件。")
        return

    user_text = event.message.text.strip()
    if not user_text:
        return

    source = event.source
    if getattr(source, "type", None) != "user" or not getattr(source, "user_id", None):
        return

    user_id = source.user_id
    user_text_lower = user_text.lower()

    # 1. 攔截：地圖請求（第 1 格）
    if is_map(user_text):
        try:
            _reply(event.reply_token, [
                TextMessage(text="[文藻外語大學校園平面圖]"),
                ImageMessage(
                    original_content_url=CAMPUS_MAP_URL,
                    preview_image_url=CAMPUS_MAP_URL,
                ),
            ])
            logger.info("已成功傳送校園地圖圖片。")
            return
        except Exception:
            logger.exception("傳送地圖圖片失敗，改用文字連結備援。")
            try:
                _reply(event.reply_token, [
                    TextMessage(text=f"[文藻外語大學校園平面圖]\n{CAMPUS_MAP_URL}")
                ])
                return
            except Exception:
                logger.exception("傳送地圖連結失敗")

    # 2. 攔截：Rich Menu 第 6 格（其他服務）
    if user_text_lower in ["其他服務", "更多服務", "選單", "menu", "more services",
                           "danh mục", "danh muc", "xem thêm"]:
        try:
            _reply(event.reply_token, [
                TextMessage(
                    text="[UniBridge] 請選擇您需要的其他服務：",
                    quick_reply=create_quick_reply(),
                )
            ])
            logger.info("已成功傳送 Quick Reply 選單。")
            return
        except Exception:
            logger.exception("傳送 Quick Reply 失敗")

    # 3. 攔截：簡單問候語（"hi"、"hello"、"chào"、"👋"）
    if user_text_lower in GREETING_KEYWORDS:
        greeting_reply = "👋 您好！我是 UniBridge AI 小幫手。您可以點選下方選單，或直接輸入問題詢問我喔！"
        try:
            _reply(event.reply_token, [TextMessage(text=greeting_reply)])
            logger.info("已成功傳送問候語。")
            return
        except Exception:
            logger.exception("傳送問候語失敗")

    # 4. 一般問答：進入 AI 流程
    try:
        reply_content = ask_ai(user_id, user_text)
        # 先截斷再附加免責聲明，避免聲明被 5000 字上限切掉
        reply_content = (
            reply_content[:5000 - len(DISCLAIMER_TEXT)] + DISCLAIMER_TEXT
        )
    except Exception:
        logger.exception("AI 處理失敗")
        reply_content = "不好意思，UniBridge 目前暫時無法回答，請稍後再試一次喔！"

    try:
        _reply(event.reply_token, [TextMessage(text=reply_content)])
    except Exception:
        logger.exception("傳送 LINE 訊息失敗")

# --- 6.5 預設 Fallback Handler ---
@handler.add(MessageEvent)
def handle_default_message(event):
    reply_text = "[UniBridge] 目前以文字訊息的支援最完整，請直接輸入文字問題，謝謝！"
    try:
        _reply(event.reply_token, [TextMessage(text=reply_text)])
        logger.info("已處理 Default Message fallback。")
    except Exception:
        logger.exception("Fallback 回覆失敗")

# ==========================================
# 7. 啟動伺服器
# ==========================================

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False)