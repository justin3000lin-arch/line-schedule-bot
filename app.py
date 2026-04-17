import os, json, uuid, re, requests
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler      = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

NOTION_API_KEY     = os.environ['NOTION_API_KEY']
NOTION_DATABASE_ID = os.environ['NOTION_DATABASE_ID']
NOTION_API_URL     = 'https://api.notion.com/v1/pages'
NOTION_HEADERS     = {
    'Authorization': f'Bearer {NOTION_API_KEY}',
    'Content-Type': 'application/json',
    'Notion-Version': '2022-06-28',
}

pending = {}
UPDATE_KEYWORDS = ['調整', '提早', '修正', '更改', '改期', '延後', '改為', '改到']

def parse_event(text):
    now = datetime.now()
    year = now.year
    is_update = any(kw in text for kw in UPDATE_KEYWORDS)
    date_str = None
    m = re.search(r'(\d{1,2})[/月](\d{1,2})(?:[日號])?(?:\([一二三四五六日天週周末假]\))?', text)
    if m:
        try:
            date_str = datetime(year, int(m.group(1)), int(m.group(2))).strftime('%Y-%m-%d')
        except ValueError:
            pass
    if not date_str:
        if '今天' in text or '今日' in text:
            date_str = now.strftime('%Y-%m-%d')
        elif '明天' in text or '明日' in text:
            date_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        elif '後天' in text:
            date_str = (now + timedelta(days=2)).strftime('%Y-%m-%d')
    start_time = end_time = None
    time_m = re.search(r'(上午|早上|早|下午|晚上|中午|凌晨)?(\d{1,2})點(?:(\d{1,2})分|(半)|(刻))?', text)
    if time_m:
        prefix = time_m.group(1) or ''
        hour = int(time_m.group(2))
        min_dig, is_half, is_q = time_m.group(3), time_m.group(4), time_m.group(5)
        minute = 30 if is_half else 15 if is_q else int(min_dig) if min_dig else 0
        if prefix in ('下午', '晚上') and hour < 12:
            hour += 12
        elif prefix in ('上午', '早上', '早') and hour == 12:
            hour = 0
        elif prefix == '中午' and hour <= 12:
            hour = 12
        elif not prefix and 1 <= hour <= 6:
            hour += 12
        start_time = f'{hour:02d}:{minute:02d}'
        end_time = f'{(hour + 1 if hour < 23 else 23):02d}:{minute:02d}'
    location = None
    loc_m = re.search(r'在(.{2,20}?)(?:開|舉行|進行|討論|舉辦|$)', text)
    if loc_m:
        lc = loc_m.group(1).strip()
        if lc and not re.match(r'^[之前後]', lc):
            location = lc
    title = None
    for pat in [
        r'開([^\s，。,]{2,20}(?:會議|討論|報告|簡報|審查|評審|說明|檢討|協調|座談|培訓|研討))',
        r'([^\s在，。,\d]{2,20}(?:會議|討論|報告|簡報|審查|評審|說明|檢討|協調|座談|培訓|研討))',
        r'開([^\s，。,]{2,15})',
    ]:
        tm = re.search(pat, text)
        if tm:
            title = tm.group(1).strip()
            break
    if not title:
        title = '行程'
    schedule_indicators = ['訂於', '安排', '會議', '討論', '報告', '簡報', '審查', '說明', '開會', '參加', '舉行', '出席', '座談', '研討', '培訓']
    is_schedule = bool(date_str or start_time) or any(i in text for i in schedule_indicators)
    result = {
        'is_schedule': is_schedule,
        'is_update': is_update,
        'title': title,
        'date': date_str,
        'start_time': start_time,
        'end_time': end_time,
        'location': location,
    }
    print(f'[PARSED] {json.dumps(result, ensure_ascii=False)}', flush=True)
    return result

def format_event_text(info):
    lines = [f'📌 {info["title"]}']
    if info.get('date') and info.get('start_time'):
        lines.append(f'📆 {info["date"]} {info["start_time"]}')
    elif info.get('date'):
        lines.append(f'📆 {info["date"]}')
    if info.get('location'):
        lines.append(f'📍 {info["location"]}')
    return '\n'.join(lines)

def save_to_notion(info):
    title_text = info['title']
    if info.get('date') and info.get('start_time'):
        title_text += f' {info["date"]} {info["start_time"]}'
    elif info.get('date'):
        title_text += f' {info["date"]}'
    if info.get('location'):
        title_text += f' @{info["location"]}'

    properties = {
        'Name': {
            'title': [{'text': {'content': title_text}}]
        }
    }

    if info.get('date'):
        date_val = info['date']
        if info.get('start_time'):
            date_val = f'{info["date"]}T{info["start_time"]}:00+08:00'
            if info.get('end_time'):
                properties['Date'] = {
                    'date': {
                        'start': date_val,
                        'end': f'{info["date"]}T{info["end_time"]}:00+08:00'
                    }
                }
            else:
                properties['Date'] = {'date': {'start': date_val}}
        else:
            properties['Date'] = {'date': {'start': date_val}}

    if info.get('location'):
        properties['Location'] = {
            'rich_text': [{'text': {'content': info['location']}}]
        }

    body = {
        'parent': {'database_id': NOTION_DATABASE_ID},
        'properties': properties,
    }
    resp = requests.post(NOTION_API_URL, headers=NOTION_HEADERS, json=body, timeout=10)
    print(f'[NOTION] status={resp.status_code} body={resp.text[:200]}', flush=True)
    if resp.status_code not in (200, 201):
        raise Exception(f'Notion API error {resp.status_code}: {resp.text[:100]}')
    return resp.json()

@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    print(f'[MSG] {text}', flush=True)

    if user_id in pending:
        info = pending.pop(user_id)
        if text in ['是', 'Y', 'y', 'yes', '確認', '好']:
            try:
                save_to_notion(info)
                verb = '已更新' if info.get('is_update') else '已記錄'
                reply = f'✅ {verb}到 Notion！\n{format_event_text(info)}'
            except Exception as ex:
                print(f'[ERR] {ex}', flush=True)
                reply = f'❌ 記錄失敗：{str(ex)[:100]}'
        else:
            reply = '已取消，不新增行程。'
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    info = parse_event(text)
    if not info['is_schedule']:
        return

    pending[user_id] = info
    verb = '更新' if info.get('is_update') else '新增'
    confirm = f'偵測到行程，確認{verb}到 Notion？\n{format_event_text(info)}\n\n回覆「是」確認，其他內容取消。'
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=confirm))

@app.route('/health', methods=['GET'])
def health():
    return json.dumps({'status': 'ok', 'version': 'notion-regex-v1'}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
