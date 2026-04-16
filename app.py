import os
import json
import uuid
from datetime import datetime, timedelta, date as date_type
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import caldav
from icalendar import Calendar, Event
import anthropic

app = Flask(__name__)

# -- 環境變數 -------------------------------------------
line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler      = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])
claude       = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

APPLE_ID       = os.environ['APPLE_ID']
APPLE_PASSWORD = os.environ['APPLE_APP_PASSWORD']
CALDAV_URL     = 'https://caldav.icloud.com'

# -- 待確認刪除的暫存 {chat_id: {old_event, old_summary}} --
pending = {}

# -- 更新行程的關鍵字 ------------------------------------
UPDATE_KEYWORDS = ['調整', '提早', '修正', '更改', '改期', '延後', '改為', '改到']


# -- 取得 iCloud 行事曆 -----------------------------------
def get_calendar():
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=APPLE_ID,
        password=APPLE_PASSWORD
    )
    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        raise Exception('找不到任何行事曆')
    for cal in calendars:
        name = str(cal.name or '')
        if any(k in name for k in ['行事曆', 'Calendar', 'Home', '家庭', '個人']):
            return cal
    return calendars[0]


# -- 用 Claude 解析行程訊息 --------------------------------
def parse_event(text):
    today = datetime.now().strftime('%Y-%m-%d')
    year  = datetime.now().year

    resp = claude.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=600,
        messages=[{
            'role': 'user',
            'content': f'從以下 LINE 訊息提取行程資訊。今天 {today}，未指定年份時用 {year}。\n\n訊息：{text}\n\n只回傳 JSON，不要其他文字：\n{{\n  "is_schedule": true/false,\n  "is_update": true/false,\n  "title": "行程標題（簡短）",\n  "date": "YYYY-MM-DD 或 null",\n  "start_time": "HH:MM 或 null（24小時制）",\n  "end_time": "HH:MM 或 null",\n  "location": "地點 或 null",\n  "original_title": "若是更新，原本行程的關鍵字 或 null",\n  "original_date": "YYYY-MM-DD 或 null"\n}}'
        }]
    )

    raw = resp.content[0].text.strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
    return json.loads(raw.strip())


# -- 建立行事曆事件 ----------------------------------------
def create_event(calendar, data):
    cal = Calendar()
    cal.add('prodid', '-//LINE行程Bot//TW')
    cal.add('version', '2.0')

    ev = Event()
    ev.add('summary', data['title'])
    ev['uid'] = str(uuid.uuid4()) + '@linebot'

    if data.get('date'):
        d = datetime.strptime(data['date'], '%Y-%m-%d')
        if data.get('start_time'):
            start = datetime.strptime(f"{data['date']} {data['start_time']}", '%Y-%m-%d %H:%M')
            if data.get('end_time'):
                end = datetime.strptime(f"{data['date']} {data['end_time']}", '%Y-%m-%d %H:%M')
            else:
                end = start + timedelta(hours=1)
            ev.add('dtstart', start)
            ev.add('dtend',   end)
        else:
            ev.add('dtstart', d.date())
            ev.add('dtend',   (d + timedelta(days=1)).date())

    if data.get('location'):
        ev.add('location', data['location'])

    cal.add_component(ev)
    calendar.add_event(cal.to_ical())
    print(f'[CALDAV] Created: {data["title"]} on {data.get("date")}', flush=True)


# -- 搜尋符合標題的舊行程 -----------------------------------
def find_old_events(calendar, title_keyword, original_date=None):
    try:
        if original_date:
            base  = datetime.strptime(original_date, '%Y-%m-%d')
            start = base - timedelta(days=3)
            end   = base + timedelta(days=3)
        else:
            start = datetime.now() - timedelta(days=30)
            end   = datetime.now() + timedelta(days=180)

        events = calendar.date_search(start=start, end=end)
        matches = []
        for ev in events:
            try:
                ev.load()
                cal = Calendar.from_ical(ev.data)
                for comp in cal.walk():
                    if comp.name == 'VEVENT':
                        summary = str(comp.get('summary', ''))
                        if title_keyword and title_keyword in summary:
                            matches.append((ev, summary))
            except Exception:
                continue
        return matches
    except Exception as e:
        print(f'[CALDAV] Search error: {e}', flush=True)
        return []


# -- 格式化回覆訊息 ----------------------------------------
def format_event_text(data):
    lines = [f'📌 {data["title"]}']
    if data.get('date'):
        time_str = f' {data["start_time"]}' if data.get('start_time') else ''
        lines.append(f'📆 {data["date"]}{time_str}')
    if data.get('location'):
        lines.append(f'📍 {data["location"]}')
    return '\n'.join(lines)


# -- 路由 -------------------------------------------------
@app.route('/')
def index():
    return '📅 LINE 行程 Bot 運行中 ✅'


@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    print(f'[CALLBACK] body length={len(body)}', flush=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f'[CALLBACK] Error: {e}', flush=True)
    return 'OK'


# -- 訊息處理 ----------------------------------------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        _handle(event)
    except Exception as e:
        print(f'[ERROR] {e}', flush=True)
        import traceback; traceback.print_exc()


def _handle(event):
    text    = event.message.text.strip()
    source  = event.source
    user_id = getattr(source, 'user_id', None)
    group_id= getattr(source, 'group_id', None)
    chat_id = group_id or user_id
    print(f'[MSG] {text[:60]!r}', flush=True)

    # -- 確認刪除 ------------------------------------------
    if text in ('是', '刪', '刪除', 'yes', 'YES', '確認', '好'):
        if chat_id in pending:
            p = pending.pop(chat_id)
            try:
                p['old_event'].delete()
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f'🗑️ 已刪除舊行程：{p["old_summary"]}\n✅ 新行程已保留在行事曆中')
                )
            except Exception as e:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f'❌ 刪除失敗：{str(e)[:100]}')
                )
        return

    # -- 取消刪除 ------------------------------------------
    if text in ('否', '不', '取消', 'no', 'NO', '不用', '保留'):
        if chat_id in pending:
            pending.pop(chat_id)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='👌 已保留舊行程，不做刪除')
            )
        return

    # -- 解析行程 ------------------------------------------
    try:
        data = parse_event(text)
        print(f'[PARSED] {json.dumps(data, ensure_ascii=False)}', flush=True)
    except Exception as e:
        print(f'[PARSE ERROR] {e}', flush=True)
        return

    if not data.get('is_schedule') or not data.get('date'):
        return

    # -- 連接行事曆 ----------------------------------------
    try:
        calendar = get_calendar()
    except Exception as e:
        print(f'[CALDAV ERROR] {e}', flush=True)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f'❌ 無法連接 Apple Calendar\n{str(e)[:100]}')
        )
        return

    # -- 判斷是否為更新行程 ----------------------------------
    is_update = data.get('is_update') or any(kw in text for kw in UPDATE_KEYWORDS)

    if is_update:
        create_event(calendar, data)

        search_kw   = data.get('original_title') or data.get('title', '')
        old_matches = find_old_events(calendar, search_kw, data.get('original_date'))

        if old_matches:
            old_event, old_summary = old_matches[0]
            pending[chat_id] = {'old_event': old_event, 'old_summary': old_summary}

            reply = (
                f'📅 新行程已加入：\n{format_event_text(data)}\n\n'
                f'🔍 找到舊行程：「{old_summary}」\n'
                f'要刪除舊的嗎？\n回覆「是」或「刪」確認 ／ 「否」保留'
            )
        else:
            reply = (
                f'📅 行程已更新加入：\n{format_event_text(data)}\n\n'
                f'（未找到符合的舊行程）'
            )
    else:
        create_event(calendar, data)
        reply = f'✅ 已加入 Apple 行事曆！\n{format_event_text(data)}'

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )


# -- 啟動 -------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
