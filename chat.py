"""
chat.py — Stage 4: interactive command-line assistant.

Keeps conversation history in memory for the session so the model
remembers context (e.g. "what about tomorrow?" after asking about today).
History is lost when you exit — this is intentional for a daily-use tool
where you want a clean slate each session.

Run with:
    venv/Scripts/python chat.py
"""

from agent import run_agent, MODEL

BANNER = f"""
╔══════════════════════════════════════╗
║   Personal Assistant  ({MODEL})
║   Type 'exit' or Ctrl-C to quit.    ║
║   Type 'clear' to reset history.    ║
╚══════════════════════════════════════╝"""


def main():
    print(BANNER)

    # history holds only user/assistant turns — not the system prompt
    # (that's added fresh each call inside run_agent so today's date stays current).
    history: list[dict] = []

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue

        if user_input.lower() == "exit":
            print("Bye.")
            break

        if user_input.lower() == "clear":
            history.clear()
            print("  (history cleared)")
            continue

        # Run the agent loop, passing the accumulated history for context.
        answer = run_agent(user_input, history=history)
        print(f"\nAssistant: {answer}")

        # Append this exchange to history so the next turn has context.
        history.append({"role": "user",      "content": user_input})
        history.append({"role": "assistant", "content": answer})


if __name__ == "__main__":
    main()
