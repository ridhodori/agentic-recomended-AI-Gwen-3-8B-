"""
Throwaway spike: does before_tool_callback/after_tool_callback registered on a
SPECIALIST Agent actually fire for its own internal tool calls when that
specialist runs via mode="single_turn" + sub_agents=[...] (run_node, inline
execution), as opposed to a separately-run Runner/session?

Also checks: does the ROOT agent's own callback fire for the delegation call
itself (treating the specialist as a "tool" named after the specialist)?
"""
import asyncio

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

MODEL_LLM = "ollama_chat/qwen3-agent:latest"

events_log = []


def make_before(tag):
    def _before(tool, args, tool_context):
        events_log.append(f"BEFORE[{tag}] tool={tool.name} args={args}")
    return _before


def make_after(tag):
    def _after(tool, args, tool_context, tool_response):
        events_log.append(f"AFTER[{tag}] tool={tool.name} response={str(tool_response)[:80]}")
    return _after


def dummy_lookup(item: str) -> str:
    """Cari harga dummy untuk sebuah item.

    Args:
      item: nama item yang dicari.

    Returns:
      str: harga dummy dalam format tetap.
    """
    return f"{item} harganya Rp12345 (dummy)"


specialist = Agent(
    model=LiteLlm(model=MODEL_LLM, num_ctx=4096),
    name="dummy_specialist",
    description="Spesialis dummy untuk mencari harga item.",
    instruction="Kamu spesialis pencarian harga. Selalu panggil tool dummy_lookup untuk menjawab pertanyaan harga.",
    tools=[dummy_lookup],
    mode="single_turn",
    before_tool_callback=make_before("SPECIALIST"),
    after_tool_callback=make_after("SPECIALIST"),
)

root = Agent(
    model=LiteLlm(model=MODEL_LLM, num_ctx=4096),
    name="dummy_router",
    description="Router dummy.",
    instruction="Kamu router. Untuk pertanyaan soal harga item, delegasikan ke dummy_specialist dengan meneruskan pertanyaan pengguna sebagai 'request'.",
    sub_agents=[specialist],
    before_tool_callback=make_before("ROOT"),
    after_tool_callback=make_after("ROOT"),
)

APP_NAME = "callback_probe"


async def main():
    ss = InMemorySessionService()
    await ss.create_session(app_name=APP_NAME, user_id="u", session_id="s1")
    runner = Runner(agent=root, app_name=APP_NAME, session_service=ss)
    content = types.Content(role="user", parts=[types.Part(text="berapa harga sabun ajaib?")])
    final_text = ""
    async for event in runner.run_async(user_id="u", session_id="s1", new_message=content):
        if event.is_final_response() and event.content and event.content.parts:
            texts = [p.text for p in event.content.parts if p.text and not p.thought]
            if texts:
                final_text = "\n".join(texts)

    print("=== root_agent.tools (auto-populated?) ===")
    print([t.name if hasattr(t, "name") else t for t in root.tools])
    print()
    print("=== FINAL ANSWER ===")
    print(final_text)
    print()
    print("=== CALLBACK EVENTS (order matters) ===")
    for e in events_log:
        print(e)
    print()
    print("=== VERDICT ===")
    print("ROOT callback fired for delegation call:", any("ROOT" in e and "dummy_specialist" in e for e in events_log))
    print("SPECIALIST callback fired for internal tool call:", any("SPECIALIST" in e and "dummy_lookup" in e for e in events_log))


if __name__ == "__main__":
    asyncio.run(main())
