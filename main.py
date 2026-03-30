import os, json, asyncio, websockets, re
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
load_dotenv()
app = FastAPI()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
active_calls = {}


def get_cancel_prompt(lang, info):
    if lang == "ja":
        return f"""あなたはプロの予約キャンセルアシスタントです。レストランに電話して予約をキャンセルします。
キャンセル情報：日付{info.get('date','')} {info.get('time','')}、{info.get('guests','')}名、{info.get('name','')}名義
指示：
- 自動音声メニューが流れたら「PRESS:番号」で対応してください
- 人間が出たら丁寧にキャンセルをお願いしてください
- 完了したら「キャンセル完了しました」、できなければ「キャンセルできませんでした」と言ってください"""
    else:
        return f"""당신은 전문 예약 취소 어시스턴트입니다. 레스토랑에 전화해서 예약을 취소합니다.
취소 정보：날짜 {info.get('date','')} {info.get('time','')}、{info.get('guests','')}명、{info.get('name','')} 이름
지시사항：
- 자동 음성 메뉴가 나오면 「PRESS:번호」로 대응하세요
- 사람이 나오면 정중하게 취소를 부탁하세요
- 완료시 「취소가 완료되었습니다」、실패시 「취소가 되지 않았습니다」라고 하세요"""

def get_prompt(lang, info):
    base = f"""
予約情報：日付{info['date']} {info['time']}、{info['guests']}名、{info['name']}名義、連絡先{info['contact']}、特別リクエスト：{info.get('requests','なし')}

重要指示：
- 自動音声メニューが流れたら、必要なボタンを「PRESS:1」「PRESS:2」のように言ってください（例：「予約は1番です、PRESS:1」）
- 人間が出たら普通に日本語で話してください
- 予約完了したら「予約完了しました」、できなければ「予約できませんでした」と言ってください
""" if lang == "ja" else f"""
예약 정보：날짜 {info['date']} {info['time']}、{info['guests']}명、{info['name']} 이름、연락처 {info['contact']}、특별 요청：{info.get('requests','없음')}

중요 지시：
- 자동 음성 메뉴가 나오면 필요한 버튼을 「PRESS:1」「PRESS:2」처럼 말하세요（예：「예약은 1번입니다、PRESS:1」）
- 사람이 나오면 정중한 한국어로 대화하세요
- 완료시 「예약이 완료되었습니다」、실패시 「예약이 되지 않았습니다」라고 하세요
"""
    return base


@app.get("/debug-env")
async def debug_env():
    import os
    return {"sid": os.getenv("TWILIO_ACCOUNT_SID", "NOT_FOUND")[:10],"token": os.getenv("TWILIO_AUTH_TOKEN", "NOT_FOUND")[:10]}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/make-call")
async def make_call(request: Request):
    data = await request.json()
    phone = data.get("phone","").replace(" ","").replace("-","")
    if not phone:
        return JSONResponse({"error": "电话号码不能为空"}, status_code=400)
    active_calls[phone] = {**data, "transcript": [], "status": "calling"}
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=phone, from_=TWILIO_PHONE_NUMBER,
            url=f"{PUBLIC_URL}/twiml?phone={phone}",
            status_callback=f"{PUBLIC_URL}/call-status?phone={phone}",
            status_callback_method="POST"
        )
        active_calls[phone]["call_sid"] = call.sid
        return JSONResponse({"success": True, "call_sid": call.sid})
    except Exception as e:
        del active_calls[phone]
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/twiml")
async def twiml(request: Request):
    phone = request.query_params.get("phone","")
    r = VoiceResponse()
    c = Connect()
    c.stream(url=f"wss://{PUBLIC_URL.replace('https://','')}/media-stream?phone={phone}")
    r.append(c)
    return HTMLResponse(content=str(r), media_type="application/xml")

@app.post("/call-status")
async def call_status(request: Request):
    phone = request.query_params.get("phone","")
    form = await request.form()
    if phone in active_calls:
        active_calls[phone]["status"] = form.get("CallStatus","")
    return JSONResponse({"ok": True})

@app.get("/call-status-check")
async def check(phone: str):
    p = phone.replace(" ","").replace("-","")
    return JSONResponse(active_calls.get(p, {"status": "not_found"}))

@app.post("/send-dtmf")
async def send_dtmf(request: Request):
    data = await request.json()
    phone = data.get("phone","").replace(" ","").replace("-","")
    digits = data.get("digits","")
    if phone not in active_calls or "call_sid" not in active_calls[phone]:
        return JSONResponse({"error": "通话不存在"}, status_code=404)
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.calls(active_calls[phone]["call_sid"]).update(
            twiml=f'<Response><Play digits="{digits}"/><Connect><Stream url="wss://{PUBLIC_URL.replace("https://","")}/media-stream?phone={phone}"/></Connect></Response>'
        )
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    phone = websocket.query_params.get("phone","")
    info = active_calls.get(phone, {})
    lang = info.get("language","ja")
    mode = info.get('mode', 'book')
    prompt = get_cancel_prompt(lang, info) if mode == 'cancel' else get_prompt(lang, info)
    voice = "shimmer" if lang == "ja" else "nova"
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}
    try:
        async with websockets.connect(url, additional_headers=headers) as ows:
            await ows.send(json.dumps({"type":"session.update","session":{"turn_detection":{"type":"server_vad"},"input_audio_format":"g711_ulaw","output_audio_format":"g711_ulaw","voice":voice,"instructions":prompt,"modalities":["text","audio"],"temperature":0.7,"input_audio_transcription":{"model":"whisper-1"}}}))
            await ows.send(json.dumps({"type":"conversation.item.create","item":{"type":"message","role":"user","content":[{"type":"input_text","text":"電話がつながりました。自動音声の場合はPRESS:番号で対応し、人間が出たら予約をお願いしてください。" if lang=="ja" else "전화가 연결되었습니다. 자동 음성이면 PRESS:번호로 대응하고, 사람이 나오면 예약을 부탁하세요."}]}}))
            await ows.send(json.dumps({"type":"response.create"}))
            sid = None

            async def from_twilio():
                nonlocal sid
                try:
                    async for msg in websocket.iter_text():
                        d = json.loads(msg)
                        if d["event"] == "media":
                            await ows.send(json.dumps({"type":"input_audio_buffer.append","audio":d["media"]["payload"]}))
                        elif d["event"] == "start":
                            sid = d["start"]["streamSid"]
                        elif d["event"] == "stop":
                            break
                except: pass

            async def to_twilio():
                try:
                    async for msg in ows:
                        r = json.loads(msg)
                        if r.get("type") == "response.audio.delta" and sid:
                            await websocket.send_text(json.dumps({"event":"media","streamSid":sid,"media":{"payload":r["delta"]}}))
                        if r.get("type") == "conversation.item.input_audio_transcription.completed":
                            t = r.get("transcript","")
                            if t and phone in active_calls:
                                active_calls[phone]["transcript"].append({"speaker":"restaurant","text":t})
                        if r.get("type") == "response.audio_transcript.done":
                            t = r.get("transcript","")
                            if t and phone in active_calls:
                                active_calls[phone]["transcript"].append({"speaker":"ai","text":t})
                                # Auto DTMF detection
                                press = re.findall(r'PRESS:([0-9#*]+)', t)
                                if press and phone in active_calls and "call_sid" in active_calls[phone]:
                                    try:
                                        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                                        client.calls(active_calls[phone]["call_sid"]).update(
                                            twiml=f'<Response><Play digits="{press[0]}"/><Connect><Stream url="wss://{PUBLIC_URL.replace("https://","")}/media-stream?phone={phone}"/></Connect></Response>'
                                        )
                                        active_calls[phone]["transcript"].append({"speaker":"sys","text":f"🔢 自动按键：{press[0]}"})
                                    except: pass
                                # completion check
                                done_ja = ["予約完了","予約できませんでした"]
                                done_ko = ["예약이 완료","예약이 되지 않았습니다"]
                                done = done_ja if lang=="ja" else done_ko
                                if any(k in t for k in done):
                                    active_calls[phone]["status"] = "completed_success" if ("完了" in t or "완료" in t) else "completed_failed"
                except: pass

            await asyncio.gather(from_twilio(), to_twilio())
    except Exception as e:
        if phone in active_calls:
            active_calls[phone]["status"] = "error"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT",8000)))
