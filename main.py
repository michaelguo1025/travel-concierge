import os, json, asyncio, websockets
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
load_dotenv()
app = FastAPI()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
active_calls = {}
def get_prompt(lang, info):
    if lang == "ja":
        return f"あなたはプロの予約アシスタントです。{info['restaurant_name']}に電話して{info['date']} {info['time']}、{info['guests']}名で{info['name']}名義の予約を取ってください。連絡先：{info['contact']}。特別リクエスト：{info.get('requests','なし')}。予約完了したら「予約完了しました」、できなければ「予約できませんでした」と言ってください。"
    return f"당신은 전문 예약 어시스턴트입니다. {info['restaurant_name']}에 전화해서 {info['date']} {info['time']}, {info['guests']}명, {info['name']} 이름으로 예약해주세요. 연락처: {info['contact']}. 특별 요청: {info.get('requests','없음')}. 완료시 '예약이 완료되었습니다', 실패시 '예약이 되지 않았습니다'라고 하세요."
@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()
@app.post("/make-call")
async def make_call(request: Request):
    data = await request.json()
    phone = data.get("phone","").replace(" ","")
    active_calls[phone] = {**data, "transcript": [], "status": "calling"}
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(to=phone, from_=TWILIO_PHONE_NUMBER, url=f"{PUBLIC_URL}/twiml?phone={phone}", status_callback=f"{PUBLIC_URL}/call-status?phone={phone}", status_callback_method="POST")
        active_calls[phone]["call_sid"] = call.sid
        return JSONResponse({"success": True, "call_sid": call.sid})
    except Exception as e:
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
    p = phone.replace(" ","")
    return JSONResponse(active_calls.get(p, {"status": "not_found"}))
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    phone = websocket.query_params.get("phone","")
    info = active_calls.get(phone, {})
    lang = info.get("language","ja")
    prompt = get_prompt(lang, info)
    voice = "shimmer" if lang == "ja" else "nova"
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}
    try:
        async with websockets.connect(url, additional_headers=headers) as ows:
            await ows.send(json.dumps({"type":"session.update","session":{"turn_detection":{"type":"server_vad"},"input_audio_format":"g711_ulaw","output_audio_format":"g711_ulaw","voice":voice,"instructions":prompt,"modalities":["text","audio"],"temperature":0.7,"input_audio_transcription":{"model":"whisper-1"}}}))
            await ows.send(json.dumps({"type":"conversation.item.create","item":{"type":"message","role":"user","content":[{"type":"input_text","text":"電話がつながりました。予約の会話を始めてください。" if lang=="ja" else "전화가 연결되었습니다. 예약 대화를 시작하세요."}]}}))
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
                                done = ["予約完了","予約できませんでした"] if lang=="ja" else ["예약이 완료","예약이 되지 않았습니다"]
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
