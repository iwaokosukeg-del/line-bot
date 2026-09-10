import os
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    FileMessageContent,
    ImageMessageContent,
    MessageEvent,
    TextMessageContent,
    VideoMessageContent,
)

import anthropic
from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHATWORK_API_TOKEN = os.environ["CHATWORK_API_TOKEN"]
CHATWORK_ROOM_ID = os.environ["CHATWORK_ROOM_ID"]
CHATWORK_MENTION = os.environ.get("CHATWORK_MENTION", "")

# システムプロンプトを読み込む
_prompt_path = Path("system_prompt.txt")
system_prompt = _prompt_path.read_text(encoding="utf-8").strip() if _prompt_path.exists() else ""

# LINE 設定
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_parser = WebhookParser(LINE_CHANNEL_SECRET)

# Anthropic 非同期クライアント
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

app = FastAPI()

# ユーザーIDをキーとして直近10件のやり取りを保持する
MAX_HISTORY = 10
conversation_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY * 2))

# 画像・動画・ファイル受信時の固定返信
DRAFT_SUBMISSION_REPLY = (
    "ご提出いただきありがとうございます😊\n"
    "内容を確認のうえ、担当者より改めてご連絡いたします🙇‍♀️"
)

# テキスト以外のメッセージ種別と日本語ラベルの対応
NON_TEXT_MESSAGE_TYPES = {
    ImageMessageContent: "画像",
    VideoMessageContent: "動画",
    FileMessageContent: "ファイル",
}

JST = ZoneInfo("Asia/Tokyo")
WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

# コード側で確実に検知し、AIの判定に関わらず必ずChatwork通知するキーワード
ESCALATION_KEYWORDS = [
    "辞退", "お断り", "キャンセル", "やめたい", "取りやめ", "降りたい", "続けられない",
    "返品", "返送", "解約", "かぶれ", "かゆい", "かゆみ", "赤み", "腫れ", "ヒリヒリ", "ピリピリ",
    "肌荒れ", "湿疹", "発疹", "体調不良", "病院", "受診", "苦情", "クレーム", "不満",
    "がっかり", "ひどい", "最悪", "二度と", "返金", "損害", "責任", "訴え",
]


def detect_escalation_keywords(text: str) -> list[str]:
    """テキストに含まれるエスカレーション対象キーワードを検出する。"""
    return [keyword for keyword in ESCALATION_KEYWORDS if keyword in text]


def build_date_notice() -> str:
    """日本時間の現在日時（今日・明日）をシステムプロンプトに追記する文字列を生成する。"""
    now = datetime.now(JST)
    tomorrow = now + timedelta(days=1)
    today_str = f"{now.year}年{now.month}月{now.day}日（{WEEKDAY_JA[now.weekday()]}）"
    tomorrow_str = f"{tomorrow.year}年{tomorrow.month}月{tomorrow.day}日（{WEEKDAY_JA[tomorrow.weekday()]}）"
    return (
        "\n\n【現在日時】\n"
        f"今日は {today_str} です。\n"
        f"明日は {tomorrow_str} です。"
    )


async def call_claude(user_id: str, user_message: str) -> str:
    """Claude API を呼び出して回答を生成する。"""
    history = conversation_history[user_id]
    messages = list(history) + [{"role": "user", "content": user_message}]

    params: dict = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": messages,
    }

    if system_prompt:
        # システムプロンプトをキャッシュして繰り返しリクエストのコストを削減する
        params["system"] = [
            {
                "type": "text",
                "text": system_prompt + build_date_notice(),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    response = await claude.messages.create(**params)

    for block in response.content:
        if block.type == "text":
            return block.text

    return ""


def format_history_for_chatwork(history: deque) -> str:
    """直近の会話履歴（3往復分）を Chatwork 通知用に整形する。"""
    recent = list(history)[-6:]  # 3往復 = ユーザー3件 + 愛子3件
    if not recent:
        return "（履歴なし）"

    lines = []
    for entry in recent:
        speaker = "ユーザー" if entry["role"] == "user" else "愛子"
        lines.append(f"{speaker}: {entry['content']}")
    return "\n".join(lines)


def build_mention_prefix() -> str:
    """CHATWORK_MENTION（"アカウントID:名前" のカンマ区切り）からメンション文字列を組み立てる。"""
    if not CHATWORK_MENTION:
        return ""

    mentions = []
    for entry in CHATWORK_MENTION.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        account_id, name = entry.split(":", 1)
        account_id = account_id.strip()
        name = name.strip()
        if account_id and name:
            mentions.append(f"[To:{account_id}]{name}さん")

    if not mentions:
        return ""
    return "".join(mentions) + "\n"


async def notify_chatwork_draft(user_id: str, message_type_label: str, history: deque) -> None:
    """画像・動画・ファイルの下書き提出を Chatwork に通知する。"""
    history_text = format_history_for_chatwork(history)
    body = (
        build_mention_prefix()
        + "[info][title]【下書き提出】確認依頼[/title]"
        f"[b]LINEユーザーID:[/b]\n{user_id}\n\n"
        f"[b]メッセージ種別:[/b]\n{message_type_label}\n\n"
        f"[b]直近の会話履歴（3往復分）:[/b]\n{history_text}[/info]"
    )
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, data={"body": body})
        resp.raise_for_status()


async def notify_chatwork(user_message: str, answer: str) -> None:
    """Chatwork の指定ルームにエスカレーション通知を送る。"""
    body = (
        build_mention_prefix()
        + "[info][title]【要確認】エスカレーション通知[/title]"
        f"[b]ユーザーメッセージ:[/b]\n{user_message}\n\n"
        f"[b]AI回答:[/b]\n{answer}[/info]"
    )
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, data={"body": body})
        resp.raise_for_status()


async def notify_chatwork_keyword_escalation(
    user_message: str, answer: str, matched_keywords: list[str]
) -> None:
    """辞退・クレーム等のキーワードを検知した際、AIの判定に関わらず Chatwork に通知する。"""
    body = (
        build_mention_prefix()
        + "[info][title]【緊急】要対応キーワード検知[/title]"
        f"[b]検知ワード:[/b] {', '.join(matched_keywords)}\n\n"
        f"[b]ユーザーメッセージ:[/b]\n{user_message}\n\n"
        f"[b]AI回答:[/b]\n{answer}[/info]"
    )
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, data={"body": body})
        resp.raise_for_status()


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        events = line_parser.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    async with AsyncApiClient(line_config) as api_client:
        line_api = AsyncMessagingApi(api_client)

        for event in events:
            if not isinstance(event, MessageEvent):
                continue

            message = event.message
            user_id = event.source.user_id

            if isinstance(message, TextMessageContent):
                user_text = message.text

                try:
                    answer = await call_claude(user_id, user_text)
                    conversation_history[user_id].append({"role": "user", "content": user_text})
                    conversation_history[user_id].append({"role": "assistant", "content": answer})
                except Exception as e:
                    answer = f"申し訳ありません、エラーが発生しました。\n{e}"

                # 辞退・クレーム等のキーワードを検知した場合は、AIの判定に関わらず必ず通知する
                # （キーワード検知と【要確認】の両方に該当する場合は、キーワード検知を優先し重複通知しない）
                matched_keywords = detect_escalation_keywords(user_text)
                needs_review = "【要確認】" in answer
                if matched_keywords:
                    try:
                        await notify_chatwork_keyword_escalation(user_text, answer, matched_keywords)
                    except Exception as e:
                        # 通知失敗はログに残すが LINE 返信には影響させない
                        print(f"Chatwork 通知エラー: {e}")
                elif needs_review:
                    try:
                        await notify_chatwork(user_text, answer)
                    except Exception as e:
                        # 通知失敗はログに残すが LINE 返信には影響させない
                        print(f"Chatwork 通知エラー: {e}")

                # LINE ユーザーに返信（【要確認】タグは除去して送る）
                line_answer = answer.replace("【要確認】", "").strip()
                await line_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=line_answer)],
                    )
                )

            elif type(message) in NON_TEXT_MESSAGE_TYPES:
                # 画像・動画・ファイル：Claude API は呼ばず、固定文で返信し、必ず Chatwork に通知する
                message_type_label = NON_TEXT_MESSAGE_TYPES[type(message)]
                marker_text = f"（{message_type_label}を送信）"

                # 文脈が途切れないよう会話履歴にも記録する
                conversation_history[user_id].append({"role": "user", "content": marker_text})
                conversation_history[user_id].append(
                    {"role": "assistant", "content": DRAFT_SUBMISSION_REPLY}
                )

                try:
                    await notify_chatwork_draft(
                        user_id, message_type_label, conversation_history[user_id]
                    )
                except Exception as e:
                    # 通知失敗はログに残すが LINE 返信には影響させない
                    print(f"Chatwork 通知エラー: {e}")

                await line_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=DRAFT_SUBMISSION_REPLY)],
                    )
                )

            else:
                continue

    return JSONResponse(content={"status": "ok"})


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "healthy"}
