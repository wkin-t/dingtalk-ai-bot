import asyncio
import aiohttp
from flask import request, Response, jsonify
from app import app
from app.config import GEMINI_API_KEY, GOOGLE_ENDPOINT, AIOHTTP_PROXY

@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "ok", "service": "Gemini Proxy"}), 200

@app.route('/v1/models', methods=['GET'])
def models():
    return jsonify({
        "object": "list",
        "data": [
            {"id": "gemini-flash-latest", "object": "model", "created": 1677610602, "owned_by": "google"},
            {"id": "gemini-3-flash-preview", "object": "model", "created": 1677610602, "owned_by": "google"}
        ]
    })

async def async_chat_completions():
    data = request.json
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GEMINI_API_KEY}"
    }
    unsupported_keys = ['frequency_penalty', 'presence_penalty', 'logit_bias', 'top_logprobs']
    safe_data = {k: v for k, v in data.items() if k not in unsupported_keys}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GOOGLE_ENDPOINT,
                json=safe_data,
                headers=headers,
                proxy=AIOHTTP_PROXY,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return Response(error_text, status=resp.status, mimetype='application/json')
                
                result = await resp.read()
                return Response(result, mimetype='application/json')

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    return asyncio.run(async_chat_completions())