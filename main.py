import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import anthropic
from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHATWORK_API_TOKEN = os.environ["CHATWORK_API_TOKEN"]
CHATWORK_ROOM_ID = os.environ["CHATWORK_ROOM_ID"]

# システムプロンプトを読み込む
_prompt_path = Path("system_prompt.txt")
system_prompt = _prompt_path.read_text(encoding="utf-8").strip() if _prompt_path.exists() else ""

# LINE 設定
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_parser = WebhookParser(LINE_CHANNEL_SECRET)

# Anthropic 非同期クライアント
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

app = FastAPI()


async def call_claude(user_message: str) -> str:
    """Claude API を呼び出して回答を生成する。"""
    params: dict = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": user_message}],
    }

    if system_prompt:
        # システムプロンプトをキャッシュして繰り返しリクエストのコストを削減する
        params["system"] = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    response = await claude.messages.create(**params)

    for block in response.content:
        if block.type == "text":
            return block.text

    return ""


async def notify_chatwork(user_message: str, answer: str) -> None:
    """Chatwork の指定ルームにエスカレーション通知を送る。"""
    body = (
        "[info][title]【要確認】エスカレーション通知[/title]"
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
            if not isinstance(event.message, TextMessageContent):
                continue

            user_text = event.message.text
            reply_token = event.reply_token

            try:
                answer = await call_claude(user_text)
            except Exception as e:
                answer = f"申し訳ありません、エラーが発生しました。\n{e}"

            # LINE ユーザーに返信
            await line_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=answer)],
                )
            )

            # 【要確認】が含まれる場合は Chatwork に通知
            if "【要確認】" in answer:
                try:
                    await notify_chatwork(user_text, answer)
                except Exception as e:
                    # 通知失敗はログに残すが LINE 返信には影響させない
                    print(f"Chatwork 通知エラー: {e}")

    return JSONResponse(content={"status": "ok"})


@app.get("/health")
async def health():
    return {"status": "healthy"}
