"""Bridges one browser call to one Gemini Live session."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from . import tools
from .store import Store

log = logging.getLogger(__name__)

GREETING = (
    "(The caller has just connected. Greet them in one short sentence "
    "and ask how you can help.)"
)

INPUT_RATE = "audio/pcm;rate=16000"


class Bridge:
    """One live call: browser websocket in, Gemini session out."""

    def __init__(
        self,
        websocket: WebSocket,
        business: tools.Business,
        store: Store,
        api_key: str,
        model: str,
        voice: str,
    ):
        self.websocket = websocket
        self.business = business
        self.store = store
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.ending = False
        self.finished = False
        self._send_lock = asyncio.Lock()

    def _config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=self.business.system_prompt(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)
                )
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            tools=tools.declarations(self.business),
        )

    async def run(self) -> None:
        client = genai.Client(api_key=self.api_key)
        try:
            async with client.aio.live.connect(
                model=self.model, config=self._config()
            ) as session:
                await self._send_json({"type": "ready", "model": self.model})
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": GREETING}]},
                    turn_complete=True,
                )
                receiver = asyncio.create_task(self._receive(session))
                try:
                    await self._pump(session)
                finally:
                    receiver.cancel()
                    try:
                        await receiver
                    except asyncio.CancelledError:
                        pass
        except WebSocketDisconnect:
            raise
        except Exception as exc:
            log.exception("live session failed")
            await self._send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    # -- browser -> gemini -------------------------------------------------

    async def _pump(self, session) -> None:
        while True:
            message = await self.websocket.receive()

            if message["type"] == "websocket.disconnect":
                return

            audio = message.get("bytes")
            if audio:
                async with self._send_lock:
                    await session.send_realtime_input(
                        audio=types.Blob(data=audio, mime_type=INPUT_RATE)
                    )
                continue

            raw = message.get("text")
            if not raw:
                continue

            try:
                command = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if command.get("type") == "text" and command.get("text"):
                async with self._send_lock:
                    await session.send_realtime_input(text=str(command["text"]))

    # -- gemini -> browser -------------------------------------------------

    async def _receive(self, session) -> None:
        while not self.finished:
            async for response in session.receive():
                await self._handle(session, response)
                if self.finished:
                    return

    async def _handle(self, session, response) -> None:
        if response.data:
            await self.websocket.send_bytes(response.data)

        content = response.server_content
        if content is not None:
            if content.interrupted:
                await self._send_json({"type": "interrupted"})

            if content.input_transcription and content.input_transcription.text:
                await self._send_json(
                    {"type": "transcript", "role": "you", "text": content.input_transcription.text}
                )

            if content.output_transcription and content.output_transcription.text:
                await self._send_json(
                    {"type": "transcript", "role": "agent", "text": content.output_transcription.text}
                )

            if content.turn_complete:
                await self._send_json({"type": "turn_complete"})
                if self.ending:
                    await self._send_json({"type": "ended"})
                    self.finished = True
                    return

        if response.tool_call:
            await self._run_tools(session, response.tool_call)

    async def _run_tools(self, session, tool_call) -> None:
        responses = []
        for call in tool_call.function_calls:
            args = dict(call.args or {})
            result = tools.execute(call.name, args, self.business, self.store)

            if call.name == "end_call" and result.get("ok"):
                self.ending = True

            log.info("tool %s(%s) -> %s", call.name, args, result)
            await self._send_json(
                {"type": "tool", "name": call.name, "args": args, "result": result}
            )

            responses.append(
                types.FunctionResponse(
                    id=call.id,
                    name=call.name,
                    response={"result": result},
                )
            )

        async with self._send_lock:
            await session.send_tool_response(function_responses=responses)

    async def _send_json(self, payload: dict) -> None:
        try:
            await self.websocket.send_text(json.dumps(payload))
        except RuntimeError:
            # socket already closed by the browser; nothing left to say
            pass
