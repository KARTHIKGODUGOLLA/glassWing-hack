# Real phone calls through Vapi (experimental)

The demo runs voice in the browser. For real phone calls, ShiftVoice exposes a Vapi
tool webhook so a phone call hits the same rules engine. **This path is not tested
end to end yet**: it needs a Vapi account, a phone number, and a public URL for the
server (for example `ngrok http 8000`).

1. Create a Vapi assistant. Set its model to a custom OpenAI-compatible LLM with base URL
   `https://api.sciforium.com/v1`, your team key, and the model string.
2. Set the assistant's server URL to `https://<your-public-host>/api/vapi/webhook`.
3. Add the two tools below. Callers are matched to staff by phone number (`staff.phone`),
   so set a staff member's number to yours in the database to try it.

```json
[
  {
    "type": "function",
    "function": {
      "name": "report_callout",
      "description": "Log an unplanned call-out (sick, emergency, bereavement). Always accepted.",
      "parameters": {
        "type": "object",
        "properties": {
          "date": {"type": "string", "description": "YYYY-MM-DD of the shift; omit for the caller's next shift"},
          "shift": {"type": "string", "enum": ["day", "night"]},
          "reason_category": {"type": "string", "enum": ["sick", "emergency", "bereavement", "unspecified"]}
        }
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "request_time_off",
      "description": "Request planned leave. The policy engine approves, sends to the supervisor, or offers alternatives.",
      "parameters": {
        "type": "object",
        "properties": {
          "start_date": {"type": "string"},
          "end_date": {"type": "string"},
          "leave_type": {"type": "string", "enum": ["vacation", "personal"]}
        },
        "required": ["start_date"]
      }
    }
  }
]
```

Suggested system prompt for the assistant: the `INBOUND_SYSTEM` ground rules in
`src/agent/prompts.py` (never deny or question a sick call, never decide anything
yourself, read back the tool result).

Outbound backfill calls over real phones are the next step: the loop in
`src/agent/backfill.py` would place calls with Vapi's `POST /call` instead of the
browser session and receive the accept/decline outcome through the same webhook.
