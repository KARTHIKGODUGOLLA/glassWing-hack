"""Stand-in for the nurse on the other end of an outbound call.

Personas are scripted by default so the demo is repeatable. With
SIM_PERSONA_LLM=1 the LLM improvises within the persona's disposition.
"""
from __future__ import annotations

import asyncio

from src import config, db
from src.agent import llm, prompts

SITUATIONS = {
    "decline": "It's early morning. You cannot take the shift today and you say so politely, with a brief reason.",
    "accept": "You are happy to pick up the extra shift and agree.",
    "ask_then_accept": "First ask whether there is an incentive. Once the caller answers, agree to take the shift.",
}


async def respond(staff: dict, turn: int, transcript: list[dict]) -> str:
    persona = db.loads(staff.get("sim_persona"), {}) or {"disposition": "accept", "lines": ["Sure, I can do it."]}
    scripted = persona["lines"][min(turn, len(persona["lines"]) - 1)]
    if not (config.SIM_PERSONA_LLM and llm.available()):
        return scripted
    system = prompts.PERSONA_SYSTEM.format(name=staff["name"], situation=SITUATIONS.get(persona["disposition"], ""))
    convo = "\n".join(f"{l['name']}: {l['text']}" for l in transcript)
    text = await asyncio.to_thread(llm.chat, [{"role": "system", "content": system},
                                              {"role": "user", "content": convo + f"\n{staff['name']}:"}],
                                   temperature=0.7, max_tokens=80)
    return (text or scripted).strip().strip('"')
