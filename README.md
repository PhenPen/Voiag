# Voiag

A voice receptionist that answers, checks a calendar, and books appointments.

Runs in the browser. One Gemini key, no other accounts.

```
you ──▶ mic ──▶ websocket ──▶ Gemini Live ──▶ tools ──▶ websocket ──▶ speakers
```

## What it does

- Hears you and replies out loud. Interruptions are off by default (see Audio below).
- Never invents availability: every slot comes from the booking logic.
- Books, finds and cancels appointments in SQLite.
- Shows the live transcript and every tool call in the page.

Seven tools: `check_availability`, `book_appointment`, `find_appointments`,
`cancel_appointment`, `get_business_info`, `take_message`, `end_call`.

## Setup

1. Get a key at https://aistudio.google.com/apikey
2. Paste it into `.env`:

   ```
   GEMINI_API_KEY=your-key
   ```

3. Install and run:

   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python run.py
   ```

4. Open http://127.0.0.1:8000 and press **Start call**.

Settings are read from `.env` once, at startup. Change the key and restart;
the console prints which key and model it loaded.

## Audio

The agent's own voice comes out of your speakers and goes back into your
microphone. Left alone, it hears its own greeting and answers itself forever.
The page stops that by going half-duplex: while the agent is talking, the mic
is muted, plus a short tail.

That means you cannot interrupt it. If you are on headphones, tick **let me
interrupt** and barge-in works normally.

Two Windows settings create the same loop even with headphones — check both if
it still talks to itself:

- **Sound → Recording → your mic → Properties → Listen** — "Listen to this
  device" must be off.
- The default input must be your microphone, not **Stereo Mix**.

## Try this

> "Hi, I'd like a consultation. Do you have anything on Thursday afternoon?"

It should check the calendar, offer times, confirm your name and number, then
book. Say "cancel that" afterwards and it will look the appointment up.

Typing works too, if the mic is unavailable.

## Config

`config/agent.json` holds the business name, opening hours, lunch break, services
and how far ahead you can book. Change it and restart. Nothing else is
business-specific.

## Check it worked

```
curl http://127.0.0.1:8000/appointments
```

## Tests

```
python -m unittest discover -s tests -t .
```

Nine tests over the booking rules: closed days, double booking, off-grid times,
cancelling with the wrong phone number, and timezone handling outside UTC.
No network, no key.

## Files

| Path | What |
| --- | --- |
| `app/live.py` | Browser websocket <-> Gemini session, tool execution |
| `app/tools.py` | Business rules, slot generation, tool declarations |
| `app/store.py` | SQLite appointments and messages |
| `web/` | Page, audio capture worklet, playback |
| `config/agent.json` | The business |

## Known limits

- The OpenCode CLI probes `localhost:8000` every 30 seconds (`GET /health`,
  then `GET /v1/models`) looking for a local model server. Those log lines are
  not your app.
- Audio is 16 kHz in, 24 kHz out, mono PCM. Works in Chrome and Edge; Safari
  does not honour the requested capture rate.
- One call per browser tab, no concurrency limits, no auth.
- Free Gemini tier is used to improve Google's products. Do not point this at
  real customer calls as-is.
- The phone line is not wired up yet. Gemini Live speaks a custom websocket
  protocol, so a phone number needs a transport in front of it (LiveKit SIP or
  Twilio Media Streams). The tool layer does not change when that lands.
