import asyncio
import json
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import httpx
from google.oauth2.service_account import Credentials as GoogleServiceAccountCredentials
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    PushMessageRequest,
    ReplyMessageRequest,
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
GOOGLE_SHEETS_CREDENTIALS = os.environ["GOOGLE_SHEETS_CREDENTIALS"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

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


# --- AI自動応答の停止機能 -----------------------------------------------
# 停止対象ユーザーはGoogleスプレッドシートで管理する。
# 1行目ヘッダー、A列:LINE USER ID / B列:氏名 / C列:メモ / D列:停止日時 / E列:停止理由

STOP_LIST_REFRESH_INTERVAL_SECONDS = 60
CHATWORK_COMMAND_POLL_INTERVAL_SECONDS = 60

# user_id -> {"name": str, "stopped_at": str, "reason": str}
stopped_users: dict[str, dict] = {}

# 二重処理防止用に処理済みのChatworkメッセージIDを記録する
processed_chatwork_message_ids: set[str] = set()

STOP_COMMAND_RE = re.compile(r"^停止\s+(\S+)(?:\s+(.+))?$", re.DOTALL)
RESUME_COMMAND_RE = re.compile(r"^再開\s+(\S+)\s*$", re.DOTALL)

_gspread_client = None


def _get_gspread_client():
    """gspreadクライアントを生成（初回のみ）してキャッシュする。"""
    global _gspread_client
    if _gspread_client is None:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        credentials = GoogleServiceAccountCredentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        _gspread_client = gspread.authorize(credentials)
    return _gspread_client


def _get_stop_list_worksheet():
    client = _get_gspread_client()
    return client.open_by_key(SPREADSHEET_ID).sheet1


def _fetch_stop_list_rows_sync() -> list[dict]:
    """スプレッドシートから停止リストの行を同期的に取得する（ブロッキング処理）。

    停止対象かどうかの判定にはA列のLINE USER IDのみを参照する。
    """
    worksheet = _get_stop_list_worksheet()
    values = worksheet.get_all_values()
    rows = []
    for row in values[1:]:
        if not row or not row[0].strip():
            continue
        rows.append(
            {
                "user_id": row[0].strip(),
                "name": row[1].strip() if len(row) > 1 else "",
                "memo": row[2].strip() if len(row) > 2 else "",
                "stopped_at": row[3].strip() if len(row) > 3 else "",
                "reason": row[4].strip() if len(row) > 4 else "",
            }
        )
    return rows


def _add_stop_row_sync(user_id: str, reason: str) -> None:
    worksheet = _get_stop_list_worksheet()
    stopped_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    worksheet.append_row([user_id, "", "", stopped_at, reason])


def _remove_stop_row_sync(user_id: str) -> bool:
    worksheet = _get_stop_list_worksheet()
    cell = worksheet.find(user_id, in_column=1)
    if cell is None:
        return False
    worksheet.delete_rows(cell.row)
    return True


async def refresh_stop_list() -> None:
    """停止リストのキャッシュをスプレッドシートから更新する。

    接続に失敗した場合は安全側（＝AIが通常どおり応答する側）に倒し、
    停止リストは空として扱う。
    """
    global stopped_users
    try:
        rows = await asyncio.to_thread(_fetch_stop_list_rows_sync)
        stopped_users = {row["user_id"]: row for row in rows}
    except Exception as e:
        print(f"停止リストの取得に失敗しました。安全側に倒して空リストとして扱います: {e}")
        stopped_users = {}


async def refresh_stop_list_loop() -> None:
    while True:
        await refresh_stop_list()
        await asyncio.sleep(STOP_LIST_REFRESH_INTERVAL_SECONDS)


async def post_to_chatwork(body: str) -> None:
    """Chatworkの指定ルームにメッセージを投稿する。"""
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, data={"body": body})
        resp.raise_for_status()


async def notify_chatwork_ai_stopped(user_id: str, received_text: str, reason: str) -> None:
    """AI応答停止中のユーザーからメッセージを受信したことをChatworkに通知する。"""
    body = (
        build_mention_prefix()
        + "[info][title]【AI停止中】メッセージ受信[/title]"
        f"[b]LINEユーザーID:[/b]\n{user_id}\n\n"
        f"[b]受信内容:[/b]\n{received_text}\n\n"
        f"[b]停止理由:[/b]\n{reason or '（理由未設定）'}[/info]"
    )
    await post_to_chatwork(body)


async def handle_stop_command(user_id: str, reason: str) -> None:
    try:
        await asyncio.to_thread(_add_stop_row_sync, user_id, reason)
        await refresh_stop_list()
        await post_to_chatwork(f"AI応答を停止しました：{user_id}")
    except Exception as e:
        print(f"停止コマンドの処理に失敗しました（user_id={user_id}）: {e}")
        try:
            await post_to_chatwork(f"AI応答の停止に失敗しました：{user_id}\nエラー: {e}")
        except Exception as notify_error:
            print(f"Chatwork 通知エラー: {notify_error}")


async def handle_resume_command(user_id: str) -> None:
    try:
        removed = await asyncio.to_thread(_remove_stop_row_sync, user_id)
        await refresh_stop_list()
        if removed:
            await post_to_chatwork(f"AI応答を再開しました：{user_id}")
        else:
            await post_to_chatwork(f"停止リストに {user_id} は見つかりませんでした。")
    except Exception as e:
        print(f"再開コマンドの処理に失敗しました（user_id={user_id}）: {e}")
        try:
            await post_to_chatwork(f"AI応答の再開に失敗しました：{user_id}\nエラー: {e}")
        except Exception as notify_error:
            print(f"Chatwork 通知エラー: {notify_error}")


async def check_chatwork_commands() -> None:
    """Chatworkルームの新着メッセージを取得し、停止・再開コマンドを処理する。"""
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages?force=1"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        messages = resp.json()

    for message in messages:
        message_id = str(message.get("message_id", ""))
        if not message_id or message_id in processed_chatwork_message_ids:
            continue

        body = (message.get("body") or "").strip()

        stop_match = STOP_COMMAND_RE.match(body)
        resume_match = RESUME_COMMAND_RE.match(body)

        if stop_match:
            processed_chatwork_message_ids.add(message_id)
            target_user_id = stop_match.group(1)
            reason = (stop_match.group(2) or "").strip()
            await handle_stop_command(target_user_id, reason)
        elif resume_match:
            processed_chatwork_message_ids.add(message_id)
            target_user_id = resume_match.group(1)
            await handle_resume_command(target_user_id)


async def chatwork_command_loop() -> None:
    while True:
        try:
            await check_chatwork_commands()
        except Exception as e:
            print(f"Chatworkコマンドの確認に失敗しました: {e}")
        await asyncio.sleep(CHATWORK_COMMAND_POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def start_background_tasks() -> None:
    asyncio.create_task(refresh_stop_list_loop())
    asyncio.create_task(chatwork_command_loop())


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


async def notify_chatwork_line_failure(user_id: str, content: str, error: str) -> None:
    """LINE送信が失敗した場合に Chatwork へ通知する。"""
    body = (
        build_mention_prefix()
        + "[info][title]【システムエラー】LINE送信失敗[/title]"
        f"[b]LINEユーザーID:[/b]\n{user_id}\n\n"
        f"[b]送信予定内容:[/b]\n{content}\n\n"
        f"[b]エラー内容:[/b]\n{error}[/info]"
    )
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, data={"body": body})
        resp.raise_for_status()


async def send_line_reply(
    line_api: AsyncMessagingApi, reply_token: str, user_id: str, text: str
) -> None:
    """LINE へ返信する。reply_message を優先し、失敗した場合のみ push_message にフォールバックする。"""
    reply_error_message = None
    try:
        await line_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )
        return
    except Exception as reply_error:
        reply_error_message = str(reply_error)
        print(f"LINE reply_message 失敗（user_id={user_id}）: {reply_error_message}")

    try:
        await line_api.push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
        )
    except Exception as push_error:
        push_error_message = str(push_error)
        print(f"LINE push_message フォールバックも失敗（user_id={user_id}）: {push_error_message}")
        error_detail = (
            f"reply_message エラー: {reply_error_message}\n"
            f"push_message エラー: {push_error_message}"
        )
        try:
            await notify_chatwork_line_failure(user_id, text, error_detail)
        except Exception as notify_error:
            print(f"Chatwork 通知エラー: {notify_error}")


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

            stop_info = stopped_users.get(user_id)
            if stop_info is not None:
                # AI自動応答停止中：Claudeは呼ばず、LINEへの返信も行わない
                if isinstance(message, TextMessageContent):
                    received_text = message.text
                elif type(message) in NON_TEXT_MESSAGE_TYPES:
                    received_text = f"（{NON_TEXT_MESSAGE_TYPES[type(message)]}を送信）"
                else:
                    continue

                conversation_history[user_id].append({"role": "user", "content": received_text})

                try:
                    await notify_chatwork_ai_stopped(
                        user_id, received_text, stop_info.get("reason", "")
                    )
                except Exception as e:
                    # 通知失敗はログに残すが処理自体は継続する
                    print(f"Chatwork 通知エラー: {e}")

                continue

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
                await send_line_reply(line_api, event.reply_token, user_id, line_answer)

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

                await send_line_reply(line_api, event.reply_token, user_id, DRAFT_SUBMISSION_REPLY)

            else:
                continue

    return JSONResponse(content={"status": "ok"})


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "healthy"}
