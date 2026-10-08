import asyncio
import json
import os
import re
import traceback
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
GLOBAL_STOP_COMMAND = "全停止"
GLOBAL_RESUME_COMMAND = "全再開"
STATUS_COMMAND = "状態"

# --- 全体停止スイッチ ----------------------------------------------------
# 全体停止の状態はスプレッドシートの「設定」シートに保存し、再起動後も維持する。
# 1行目ヘッダー、A列:項目 / B列:値（ON/OFF） / C列:更新日時
SETTINGS_SHEET_TITLE = "設定"
SETTINGS_SHEET_HEADERS = ["項目", "値", "更新日時"]
GLOBAL_STOP_SETTING_KEY = "全停止"

global_stop_enabled = False

# --- メッセージログ ------------------------------------------------------
# 1行目ヘッダー、A列:受信日時 / B列:LINE USER ID / C列:LINEプロフィール名 / D列:受信内容 / E列:AI応答の有無
MESSAGE_LOG_SHEET_TITLE = "メッセージログ"
MESSAGE_LOG_SHEET_HEADERS = ["受信日時", "LINE USER ID", "LINEプロフィール名", "受信内容", "AI応答の有無"]
LOG_STATUS_RESPONDED = "応答"
LOG_STATUS_STOPPED = "停止中"
LOG_STATUS_ERROR = "エラー"

# create_task したログ書き込みタスクがGCされないよう参照を保持する
_background_tasks: set[asyncio.Task] = set()

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


# シート名 -> Worksheet（API呼び出し回数を抑えるためキャッシュする）
_worksheet_cache: dict[str, gspread.Worksheet] = {}


def _get_or_create_worksheet_sync(title: str, headers: list[str]) -> gspread.Worksheet:
    """停止リストと同じスプレッドシート内のシートを取得し、無ければヘッダー付きで作成する。"""
    cached = _worksheet_cache.get(title)
    if cached is not None:
        return cached

    spreadsheet = _get_gspread_client().open_by_key(SPREADSHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        worksheet.append_row(headers)

    _worksheet_cache[title] = worksheet
    return worksheet


def _get_settings_worksheet() -> gspread.Worksheet:
    return _get_or_create_worksheet_sync(SETTINGS_SHEET_TITLE, SETTINGS_SHEET_HEADERS)


def _fetch_global_stop_sync() -> bool:
    """設定シートから全体停止の状態を取得する（ブロッキング処理）。"""
    worksheet = _get_settings_worksheet()
    for row in worksheet.get_all_values()[1:]:
        if row and row[0].strip() == GLOBAL_STOP_SETTING_KEY:
            return len(row) > 1 and row[1].strip().upper() == "ON"
    return False


def _set_global_stop_sync(enabled: bool) -> None:
    """設定シートに全体停止の状態を書き込む（ブロッキング処理）。"""
    worksheet = _get_settings_worksheet()
    value = "ON" if enabled else "OFF"
    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    cell = worksheet.find(GLOBAL_STOP_SETTING_KEY, in_column=1)
    if cell is not None:
        worksheet.update([[value, updated_at]], f"B{cell.row}:C{cell.row}")
    else:
        worksheet.append_row([GLOBAL_STOP_SETTING_KEY, value, updated_at])


def _append_message_log_sync(row: list[str]) -> None:
    worksheet = _get_or_create_worksheet_sync(MESSAGE_LOG_SHEET_TITLE, MESSAGE_LOG_SHEET_HEADERS)
    worksheet.append_row(row, value_input_option="RAW")


async def _write_message_log(
    received_at: str, user_id: str, display_name: str, received_text: str, status: str
) -> None:
    try:
        await asyncio.to_thread(
            _append_message_log_sync, [received_at, user_id, display_name, received_text, status]
        )
    except Exception as e:
        # 書き込み失敗はログに残すのみ。次回はシートを取り直す
        _worksheet_cache.pop(MESSAGE_LOG_SHEET_TITLE, None)
        print(f"メッセージログの書き込みに失敗しました（user_id={user_id}）: {e}")


def log_message(user_id: str, display_name: str, received_text: str, status: str) -> None:
    """受信メッセージをメッセージログシートに記録する。

    LINE返信やChatwork通知を遅らせないよう、バックグラウンドで書き込む。
    """
    received_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    task = asyncio.create_task(
        _write_message_log(received_at, user_id, display_name, received_text, status)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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


def _upsert_stop_row_sync(user_id: str, reason: str) -> bool:
    """停止リストに行を追加、または既存行があれば停止日時・理由のみ更新する。

    戻り値は「追加前から既に停止リストに存在していたか」。
    """
    worksheet = _get_stop_list_worksheet()
    stopped_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    cell = worksheet.find(user_id, in_column=1)
    if cell is not None:
        worksheet.update([[stopped_at, reason]], f"D{cell.row}:E{cell.row}")
        return True

    worksheet.append_row([user_id, "", "", stopped_at, reason])
    return False


def _remove_stop_row_sync(user_id: str) -> bool:
    worksheet = _get_stop_list_worksheet()
    cell = worksheet.find(user_id, in_column=1)
    if cell is None:
        return False
    worksheet.delete_rows(cell.row)
    return True


def _dedupe_stop_list_rows_sync() -> None:
    """同一LINE USER IDの重複行を整理し、最後（最新）の1行だけを残す。"""
    worksheet = _get_stop_list_worksheet()
    values = worksheet.get_all_values()

    last_row_by_user: dict[str, int] = {}
    for row_number, row in enumerate(values[1:], start=2):
        if not row or not row[0].strip():
            continue
        last_row_by_user[row[0].strip()] = row_number

    rows_to_delete = [
        row_number
        for row_number, row in enumerate(values[1:], start=2)
        if row and row[0].strip() and last_row_by_user[row[0].strip()] != row_number
    ]

    for row_number in sorted(rows_to_delete, reverse=True):
        worksheet.delete_rows(row_number)


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


async def refresh_global_stop() -> None:
    """全体停止の状態をスプレッドシートから更新する。

    取得に失敗した場合は直前の状態を維持する（全停止中に通信エラーで勝手に再開しないため）。
    """
    global global_stop_enabled
    try:
        global_stop_enabled = await asyncio.to_thread(_fetch_global_stop_sync)
    except Exception as e:
        _worksheet_cache.pop(SETTINGS_SHEET_TITLE, None)
        print(f"全体停止状態の取得に失敗しました。直前の状態を維持します: {e}")


async def refresh_stop_list_loop() -> None:
    while True:
        await refresh_stop_list()
        await refresh_global_stop()
        await asyncio.sleep(STOP_LIST_REFRESH_INTERVAL_SECONDS)


async def post_to_chatwork(body: str) -> None:
    """Chatworkの指定ルームにメッセージを投稿する。"""
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, data={"body": body})
        resp.raise_for_status()


# --- LINEプロフィール名の取得 ---------------------------------------------
# user_id -> displayName（メモリ上のキャッシュ。サーバー再起動でリセットされる）
line_display_name_cache: dict[str, str] = {}

DISPLAY_NAME_FETCH_FAILED = "（取得失敗）"


async def get_line_display_name(line_api: AsyncMessagingApi, user_id: str) -> str:
    """LINEのプロフィール名を取得する。失敗時は「（取得失敗）」を返し、処理は継続する。

    取得に成功した名前のみキャッシュし、失敗した場合は次回再取得を試みる。
    """
    cached = line_display_name_cache.get(user_id)
    if cached is not None:
        return cached

    try:
        profile = await line_api.get_profile(user_id)
        display_name = profile.display_name
    except Exception as e:
        print(f"LINEプロフィールの取得に失敗しました（user_id={user_id}）: {e}")
        return DISPLAY_NAME_FETCH_FAILED

    line_display_name_cache[user_id] = display_name
    return display_name


def build_user_header(display_name: str, user_id: str) -> str:
    """Chatwork通知に共通で付けるLINEユーザー名・IDのヘッダーを組み立てる。

    USER IDは担当者が停止コマンドにコピペするため、単独の行に置く。
    """
    return (
        f"[b]LINEユーザー名:[/b]\n{display_name}\n\n"
        f"[b]LINEユーザーID:[/b]\n{user_id}\n\n"
    )


def build_stop_command_hint(user_id: str) -> str:
    """通知末尾に添える停止コマンドの案内を組み立てる。"""
    return f"\n\n※自動応答を止める場合：停止 {user_id} 理由"


async def notify_chatwork_ai_stopped(
    user_id: str, display_name: str, received_text: str, reason: str
) -> None:
    """AI応答停止中のユーザーからメッセージを受信したことをChatworkに通知する。"""
    body = (
        build_mention_prefix()
        + "[info][title]【AI停止中】メッセージ受信[/title]"
        + build_user_header(display_name, user_id)
        + f"[b]受信内容:[/b]\n{received_text}\n\n"
        f"[b]停止理由:[/b]\n{reason or '（理由未設定）'}[/info]"
    )
    await post_to_chatwork(body)


async def handle_stop_command(user_id: str, reason: str) -> None:
    try:
        already_stopped = await asyncio.to_thread(_upsert_stop_row_sync, user_id, reason)
        await refresh_stop_list()
        if already_stopped:
            await post_to_chatwork(f"すでに停止中です：{user_id}")
        else:
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
            await post_to_chatwork(f"停止リストに登録されていません：{user_id}")
    except Exception as e:
        print(f"再開コマンドの処理に失敗しました（user_id={user_id}）: {e}")
        try:
            await post_to_chatwork(f"AI応答の再開に失敗しました：{user_id}\nエラー: {e}")
        except Exception as notify_error:
            print(f"Chatwork 通知エラー: {notify_error}")


async def handle_global_stop_command(enabled: bool) -> None:
    global global_stop_enabled
    action = "全停止" if enabled else "全再開"
    try:
        await asyncio.to_thread(_set_global_stop_sync, enabled)
        global_stop_enabled = enabled
        if enabled:
            await post_to_chatwork(
                "全ユーザーへのAI自動応答を停止しました。\n解除する場合：全再開"
            )
        else:
            await post_to_chatwork("全ユーザーへのAI自動応答を再開しました。")
    except Exception as e:
        _worksheet_cache.pop(SETTINGS_SHEET_TITLE, None)
        print(f"{action}コマンドの処理に失敗しました: {e}")
        try:
            await post_to_chatwork(f"{action}に失敗しました。\nエラー: {e}")
        except Exception as notify_error:
            print(f"Chatwork 通知エラー: {notify_error}")


async def handle_status_command() -> None:
    await refresh_stop_list()
    await refresh_global_stop()
    global_status = "全停止中" if global_stop_enabled else "稼働中（全停止はしていません）"
    try:
        await post_to_chatwork(
            "[info][title]現在の状態[/title]"
            f"全体: {global_status}\n"
            f"個別停止者数: {len(stopped_users)}人[/info]"
        )
    except Exception as e:
        print(f"Chatwork 通知エラー: {e}")


async def check_chatwork_commands() -> None:
    """Chatworkルームの新着メッセージを取得し、停止・再開・全停止・全再開・状態コマンドを処理する。"""
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

        if body == GLOBAL_STOP_COMMAND:
            processed_chatwork_message_ids.add(message_id)
            await handle_global_stop_command(True)
        elif body == GLOBAL_RESUME_COMMAND:
            processed_chatwork_message_ids.add(message_id)
            await handle_global_stop_command(False)
        elif body == STATUS_COMMAND:
            processed_chatwork_message_ids.add(message_id)
            await handle_status_command()
        elif stop_match:
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


async def seed_processed_chatwork_message_ids() -> None:
    """起動時点でChatworkルームに存在する既存メッセージを「処理済み」として記録する。

    これにより、起動前に投稿された古い停止・再開コマンドを実行してしまうことを防ぐ。
    """
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages?force=1"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            messages = resp.json()
    except Exception as e:
        print(f"Chatworkメッセージの初期読み込みに失敗しました: {e}")
        return

    for message in messages:
        message_id = str(message.get("message_id", ""))
        if message_id:
            processed_chatwork_message_ids.add(message_id)


@app.on_event("startup")
async def start_background_tasks() -> None:
    try:
        await asyncio.to_thread(_dedupe_stop_list_rows_sync)
    except Exception as e:
        print(f"停止リストの重複行整理に失敗しました: {e}")

    await refresh_global_stop()
    await seed_processed_chatwork_message_ids()

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


async def notify_chatwork_draft(
    user_id: str, display_name: str, message_type_label: str, history: deque
) -> None:
    """画像・動画・ファイルの下書き提出を Chatwork に通知する。"""
    history_text = format_history_for_chatwork(history)
    body = (
        build_mention_prefix()
        + "[info][title]【下書き提出】確認依頼[/title]"
        + build_user_header(display_name, user_id)
        + f"[b]メッセージ種別:[/b]\n{message_type_label}\n\n"
        f"[b]直近の会話履歴（3往復分）:[/b]\n{history_text}"
        + build_stop_command_hint(user_id)
        + "[/info]"
    )
    await post_to_chatwork(body)


async def notify_chatwork(user_id: str, display_name: str, user_message: str, answer: str) -> None:
    """Chatwork の指定ルームにエスカレーション通知を送る。"""
    body = (
        build_mention_prefix()
        + "[info][title]【要確認】エスカレーション通知[/title]"
        + build_user_header(display_name, user_id)
        + f"[b]ユーザーメッセージ:[/b]\n{user_message}\n\n"
        f"[b]AI回答:[/b]\n{answer}"
        + build_stop_command_hint(user_id)
        + "[/info]"
    )
    await post_to_chatwork(body)


async def notify_chatwork_keyword_escalation(
    user_id: str, display_name: str, user_message: str, answer: str, matched_keywords: list[str]
) -> None:
    """辞退・クレーム等のキーワードを検知した際、AIの判定に関わらず Chatwork に通知する。"""
    body = (
        build_mention_prefix()
        + "[info][title]【緊急】要対応キーワード検知[/title]"
        + build_user_header(display_name, user_id)
        + f"[b]検知ワード:[/b] {', '.join(matched_keywords)}\n\n"
        f"[b]ユーザーメッセージ:[/b]\n{user_message}\n\n"
        f"[b]AI回答:[/b]\n{answer}"
        + build_stop_command_hint(user_id)
        + "[/info]"
    )
    await post_to_chatwork(body)


async def notify_chatwork_global_stopped(
    user_id: str, display_name: str, received_text: str
) -> None:
    """全体停止中にメッセージを受信したことをChatworkに通知する。"""
    body = (
        build_mention_prefix()
        + "[info][title]【全停止中】メッセージ受信[/title]"
        + build_user_header(display_name, user_id)
        + f"[b]受信内容:[/b]\n{received_text}\n\n"
        "※全停止中のためAIは返信していません。解除する場合：全再開[/info]"
    )
    await post_to_chatwork(body)


def summarize_error(error: Exception, max_length: int = 300) -> str:
    """Chatwork通知用に例外を1行の要約にする（詳細はログにのみ出力する）。"""
    summary = f"{type(error).__name__}: {error}".replace("\n", " ")
    if len(summary) > max_length:
        summary = summary[:max_length] + "…"
    return summary


async def notify_chatwork_claude_failure(
    user_id: str,
    display_name: str,
    received_text: str,
    error_summary: str,
    matched_keywords: list[str],
) -> None:
    """Claude APIの呼び出しに失敗した場合に Chatwork へ通知する（LINEには返信しない）。"""
    keyword_line = (
        f"[b]検知ワード:[/b] {', '.join(matched_keywords)}\n\n" if matched_keywords else ""
    )
    body = (
        build_mention_prefix()
        + "[info][title]【システムエラー】応答失敗[/title]"
        + build_user_header(display_name, user_id)
        + keyword_line
        + f"[b]受信メッセージ:[/b]\n{received_text}\n\n"
        f"[b]エラー内容:[/b]\n{error_summary}\n\n"
        "※LINEには返信していません。必要に応じて手動で対応してください。"
        + build_stop_command_hint(user_id)
        + "[/info]"
    )
    await post_to_chatwork(body)


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

            if isinstance(message, TextMessageContent):
                received_text = message.text
            elif type(message) in NON_TEXT_MESSAGE_TYPES:
                received_text = f"（{NON_TEXT_MESSAGE_TYPES[type(message)]}を送信）"
            else:
                continue

            display_name = await get_line_display_name(line_api, user_id)

            if global_stop_enabled:
                # 全体停止中：Claudeは呼ばず、LINEへの返信も一切行わない
                conversation_history[user_id].append({"role": "user", "content": received_text})
                try:
                    await notify_chatwork_global_stopped(user_id, display_name, received_text)
                except Exception as e:
                    print(f"Chatwork 通知エラー: {e}")
                log_message(user_id, display_name, received_text, LOG_STATUS_STOPPED)
                continue

            stop_info = stopped_users.get(user_id)
            if stop_info is not None:
                # AI自動応答停止中：Claudeは呼ばず、LINEへの返信も行わない
                conversation_history[user_id].append({"role": "user", "content": received_text})

                try:
                    await notify_chatwork_ai_stopped(
                        user_id, display_name, received_text, stop_info.get("reason", "")
                    )
                except Exception as e:
                    # 通知失敗はログに残すが処理自体は継続する
                    print(f"Chatwork 通知エラー: {e}")

                log_message(user_id, display_name, received_text, LOG_STATUS_STOPPED)
                continue

            if isinstance(message, TextMessageContent):
                user_text = message.text
                matched_keywords = detect_escalation_keywords(user_text)

                try:
                    answer = await call_claude(user_id, user_text)
                    conversation_history[user_id].append({"role": "user", "content": user_text})
                    conversation_history[user_id].append({"role": "assistant", "content": answer})
                except Exception as e:
                    # API失敗時はLINEへ一切返信しない。詳細はログのみに出し、Chatworkには要約を通知する
                    print(f"Claude API 呼び出し失敗（user_id={user_id}）: {e}")
                    traceback.print_exc()
                    try:
                        await notify_chatwork_claude_failure(
                            user_id, display_name, user_text, summarize_error(e), matched_keywords
                        )
                    except Exception as notify_error:
                        print(f"Chatwork 通知エラー: {notify_error}")
                    log_message(user_id, display_name, received_text, LOG_STATUS_ERROR)
                    continue

                # 辞退・クレーム等のキーワードを検知した場合は、AIの判定に関わらず必ず通知する
                # （キーワード検知と【要確認】の両方に該当する場合は、キーワード検知を優先し重複通知しない）
                needs_review = "【要確認】" in answer
                if matched_keywords:
                    try:
                        await notify_chatwork_keyword_escalation(
                            user_id, display_name, user_text, answer, matched_keywords
                        )
                    except Exception as e:
                        # 通知失敗はログに残すが LINE 返信には影響させない
                        print(f"Chatwork 通知エラー: {e}")
                elif needs_review:
                    try:
                        await notify_chatwork(user_id, display_name, user_text, answer)
                    except Exception as e:
                        # 通知失敗はログに残すが LINE 返信には影響させない
                        print(f"Chatwork 通知エラー: {e}")

                # LINE ユーザーに返信（【要確認】タグは除去して送る）
                line_answer = answer.replace("【要確認】", "").strip()
                await send_line_reply(line_api, event.reply_token, user_id, line_answer)
                log_message(user_id, display_name, received_text, LOG_STATUS_RESPONDED)

            elif type(message) in NON_TEXT_MESSAGE_TYPES:
                # 画像・動画・ファイル：Claude API は呼ばず、固定文で返信し、必ず Chatwork に通知する
                message_type_label = NON_TEXT_MESSAGE_TYPES[type(message)]

                # 文脈が途切れないよう会話履歴にも記録する
                conversation_history[user_id].append({"role": "user", "content": received_text})
                conversation_history[user_id].append(
                    {"role": "assistant", "content": DRAFT_SUBMISSION_REPLY}
                )

                try:
                    await notify_chatwork_draft(
                        user_id, display_name, message_type_label, conversation_history[user_id]
                    )
                except Exception as e:
                    # 通知失敗はログに残すが LINE 返信には影響させない
                    print(f"Chatwork 通知エラー: {e}")

                await send_line_reply(line_api, event.reply_token, user_id, DRAFT_SUBMISSION_REPLY)
                log_message(user_id, display_name, received_text, LOG_STATUS_RESPONDED)

    return JSONResponse(content={"status": "ok"})


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "healthy"}
